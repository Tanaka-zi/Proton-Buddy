#!/usr/bin/env python3
"""
Proton Buddy v1.0.3 — first public release.

Scans EVERY Steam library on EVERY disk for installed games and Proton
runtimes, works out which Proton each game is configured to use in Steam
(CompatToolMapping in config.vdf), and launches companion Windows apps
(trainers, mod tools, ...) into the game's own Proton prefix.

One-click AUTO flow: pick a game, add one or more companion .exe files,
press Launch — Proton Buddy starts the game through Steam, waits until
the game process is actually running inside its prefix, then launches
each companion app into that same prefix with the same Proton.

Extras: per-game desktop shortcuts (double-click to launch game + apps),
an optional babysitter that closes the companion apps when the game
exits, and one-click save backup / restore from inside the prefix.

Headless use:
    proton_buddy.py --list
    proton_buddy.py --launch <APPID> [--app /path/to/tool.exe]

Prototype limits still apply: whether a companion app can attach to a
game depends on the app, the game, anti-cheat, and the Proton version.
"""
import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_NAME = "Proton Buddy"
VERSION = "1.0.3"
CONFIG_DIR = Path.home() / ".config/proton-buddy"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_DIR = CONFIG_DIR / "logs"
LOG_FILE = LOG_DIR / "proton_buddy.log"
BACKUP_DIR = CONFIG_DIR / "backups"

STEAM_ROOTS = [
    Path.home() / ".local/share/Steam",
    Path.home() / ".steam/steam",
    Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    Path.home() / "snap/steam/common/.local/share/Steam",
]


# ---------------------------------------------------------------- config

def read_config():
    try:
        data = json.loads(CONFIG_FILE.read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_config(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def cfg_apps(cfg, appid):
    return cfg.setdefault("apps", {}).setdefault(str(appid), [])


def cfg_game_settings(cfg, appid):
    """Per-game options: close_apps_on_exit (babysitter), save_path."""
    return cfg.setdefault("game_settings", {}).setdefault(str(appid), {})


def file_log(line):
    """Append a timestamped copy of a log line to the persistent log
    file, so headless (shortcut) launches and past sessions can be
    inspected later. Best effort — never raises."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except Exception:
        pass


def cli_log(msg):
    print(msg)
    file_log(msg)


def notify(title, body):
    """Desktop notification, best effort (used by headless launches)."""
    try:
        exe = shutil.which("notify-send")
        if exe:
            subprocess.Popen([exe, title, body])
    except Exception:
        pass


# ---------------------------------------------------------- Steam layout

def steam_roots():
    """Existing Steam installations (native + Flatpak), de-duplicated."""
    roots, seen = [], set()
    for root in STEAM_ROOTS:
        try:
            key = str(root.resolve())
        except Exception:
            key = str(root)
        if root.exists() and key not in seen:
            seen.add(key)
            roots.append(root)
    return roots


def parse_library_paths(root):
    """All library folders of one Steam root — including libraries that
    live on other disks (from libraryfolders.vdf, both locations)."""
    paths = [root]
    for rel in ("config/libraryfolders.vdf", "steamapps/libraryfolders.vdf"):
        vdf = root / rel
        if not vdf.exists():
            continue
        try:
            text = vdf.read_text(errors="replace")
        except OSError:
            continue
        for raw in re.findall(r'"path"\s*"([^"]+)"', text):
            p = Path(raw.replace("\\\\", "\\"))
            if p.exists() and p not in paths:
                paths.append(p)
    return paths


def all_libraries(roots):
    """Map: library path (str) -> owning Steam root (Path)."""
    owner = {}
    for root in roots:
        for lib in parse_library_paths(root):
            owner.setdefault(str(lib), root)
    return owner


def _vdf_block(text, key):
    """Return the {...} block that follows "key" in a VDF file."""
    idx = text.find(f'"{key}"')
    if idx < 0:
        return ""
    start = text.find("{", idx)
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def parse_compat_tool_mapping(root):
    """{appid: Proton internal name} from config.vdf; appid '0' = default.
    This is how Steam itself records which Proton each game must use."""
    vdf = root / "config" / "config.vdf"
    mapping = {}
    if not vdf.exists():
        return mapping
    try:
        text = vdf.read_text(errors="replace")
    except OSError:
        return mapping
    block = _vdf_block(text, "CompatToolMapping")
    for appid, name in re.findall(r'"(\d+)"\s*\{\s*"name"\s*"([^"]*)"', block):
        if name:
            mapping[appid] = name
    return mapping


# -------------------------------------------------------- Proton catalog

def _tool_version_key(name):
    n = name.lower()
    if "experimental" in n:
        return (999, 0)
    if "hotfix" in n:
        return (998, 0)
    m = re.search(r"(\d+)\.(\d+)", name)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d+)", name)
    if m:
        return (int(m.group(1)), 0)
    return (0, 0)


def _read_compat_tool_vdf(folder):
    """(internal name, display name) from a compatibilitytool.vdf."""
    vdf = folder / "compatibilitytool.vdf"
    internal = display = None
    if vdf.exists():
        try:
            text = vdf.read_text(errors="replace")
        except OSError:
            text = ""
        m = re.search(r'"compat_tools"\s*\{\s*"([^"]+)"', text)
        if m:
            internal = m.group(1)
        m = re.search(r'"display_name"\s*"([^"]+)"', text)
        if m:
            display = m.group(1)
    return internal, display


def discover_protons(roots, libraries):
    """Every Proton on every disk: each Steam root's compatibilitytools.d
    (GE-Proton and friends) AND the Proton folders inside every library's
    steamapps/common — not just the system disk's Steam folder."""
    tools = {}

    def add(name, internal, proton_path, kind):
        key = str(proton_path)
        if key not in tools:
            tools[key] = {"name": name, "internal": internal or "",
                          "path": key, "kind": kind}

    for root in roots:
        ctd = root / "compatibilitytools.d"
        if ctd.is_dir():
            for d in sorted(ctd.iterdir()):
                proton = d / "proton"
                if d.is_dir() and proton.is_file():
                    internal, display = _read_compat_tool_vdf(d)
                    add(display or internal or d.name, internal, proton,
                        "compatibilitytools.d")
    for lib in libraries:
        common = Path(lib) / "steamapps" / "common"
        if common.is_dir():
            for d in sorted(common.iterdir()):
                proton = d / "proton"
                if d.is_dir() and d.name.lower().startswith("proton") and proton.is_file():
                    add(d.name, "", proton, "Steam library")
    return sorted(tools.values(),
                  key=lambda t: (_tool_version_key(t["name"]), t["name"]),
                  reverse=True)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def match_tool(internal, catalog):
    """Resolve a Steam internal tool name ('proton_9', 'GE-Proton9-5',
    'proton_experimental', ...) to a discovered Proton folder."""
    if not internal:
        return None
    for t in catalog:
        if t["internal"] and t["internal"] == internal:
            return t
    ni = _norm(internal)
    for t in catalog:
        if _norm(t["name"]) == ni:
            return t
    low = internal.lower()
    for word in ("experimental", "hotfix"):
        if word in low:
            for t in catalog:
                if word in t["name"].lower():
                    return t
    digits = re.sub(r"\D", "", internal)
    if digits:
        for t in catalog:
            m = re.search(r"(\d+)\.(\d+)", t["name"])
            if m and (m.group(1) + m.group(2)) == digits:
                return t
            if m and m.group(1) == digits and m.group(2) == "0":
                return t
    return None


def auto_proton_for(appid, catalog, mapping):
    """The Proton Steam itself would use for this game, plus the reason."""
    if not catalog:
        return None, "No Proton found on any disk"
    tool = match_tool(mapping.get(str(appid), ""), catalog)
    if tool:
        return tool, f"Steam is configured to use {tool['name']} for this game"
    tool = match_tool(mapping.get("0", ""), catalog)
    if tool:
        return tool, f"Steam's default compatibility tool ({tool['name']})"
    stock = [t for t in catalog if t["kind"] == "Steam library"]
    tool = (stock or catalog)[0]
    return tool, f"Newest Proton found ({tool['name']})"


# --------------------------------------------------------- Game catalog

SKIP_NAME_BITS = ("steam linux runtime", "steamworks common redistributables",
                  "easyanticheat", "battleye")


def discover_games(roots, lib_owner):
    found = {}
    for lib_str, root in lib_owner.items():
        library = Path(lib_str)
        apps = library / "steamapps"
        if not apps.is_dir():
            continue
        for manifest in sorted(apps.glob("appmanifest_*.acf")):
            try:
                text = manifest.read_text(errors="replace")
            except OSError:
                continue
            appid_m = re.search(r'"appid"\s*"(\d+)"', text)
            name_m = re.search(r'"name"\s*"([^"]+)"', text)
            installdir_m = re.search(r'"installdir"\s*"([^"]+)"', text)
            if not (appid_m and name_m):
                continue
            appid = appid_m.group(1)
            name = name_m.group(1)
            low = name.lower()
            if low.startswith("proton") or any(b in low for b in SKIP_NAME_BITS):
                continue
            install_dir = apps / "common" / (installdir_m.group(1)
                                             if installdir_m else name)
            # Proton data normally lives beside the game, in the same
            # library; fall back to the Steam roots just in case.
            compat = apps / "compatdata" / appid
            if not (compat / "pfx").is_dir():
                for base in roots:
                    cand = base / "steamapps" / "compatdata" / appid
                    if (cand / "pfx").is_dir():
                        compat = cand
                        break
            game = {
                "appid": appid,
                "name": name,
                "library": lib_str,
                "steam_root": str(root),
                "install_dir": str(install_dir),
                "compatdata": str(compat),
                "prefix": str(compat / "pfx") if (compat / "pfx").is_dir() else "",
            }
            found[appid] = game
    return sorted(found.values(), key=lambda g: g["name"].casefold())


# --------------------------------------------------------- Launch engine

WINE_SYSTEM_EXES = {
    "services.exe", "plugplay.exe", "svchost.exe", "rpcss.exe",
    "winedevice.exe", "explorer.exe", "wineboot.exe", "winemenubuilder.exe",
    "conhost.exe", "start.exe",
}


def prefix_processes(compatdata):
    """All processes running inside this game's prefix (they carry its
    STEAM_COMPAT_DATA_PATH), with cmdline and real exe path."""
    if not compatdata:
        return []
    try:
        target = os.path.realpath(compatdata)
    except Exception:
        target = compatdata
    out = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "environ").read_bytes()
        except Exception:
            continue
        match = False
        for item in raw.split(b"\0"):
            if item.startswith(b"STEAM_COMPAT_DATA_PATH="):
                val = item.split(b"=", 1)[1].decode(errors="replace")
                try:
                    val = os.path.realpath(val)
                except Exception:
                    pass
                match = (val == target)
                break
        if not match:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            cmdline = ""
        try:
            exe = os.readlink(entry / "exe")
        except Exception:
            exe = ""
        out.append({"pid": int(entry.name), "cmdline": cmdline, "exe": exe})
    return out


def _is_game_process(p):
    cmd = p["cmdline"].lower()
    if "wineserver" in cmd or ".exe" not in cmd:
        return False
    for token in cmd.replace("\\", " ").split():
        if token.endswith(".exe"):
            if os.path.basename(token.strip('"')) not in WINE_SYSTEM_EXES:
                return True
    return False


def game_process_age(compatdata):
    """Age in seconds of the oldest live game process in the prefix,
    or None when no game process is alive. A game whose process spawned
    seconds ago is still deep in its own startup — companion apps that
    join that early can stall it (Sekiro sits in its startup phase long
    after its process exists), so AUTO waits for real process age."""
    try:
        btime = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if btime is None:
            return None
        clk = os.sysconf("SC_CLK_TCK")
    except Exception:
        return None
    ages = []
    for proc_info in prefix_processes(compatdata):
        if not _is_game_process(proc_info):
            continue
        try:
            stat = Path(f"/proc/{proc_info['pid']}/stat").read_text()
            fields = stat.rsplit(") ", 1)[1].split()
            started = btime + int(fields[19]) / clk
            ages.append(time.time() - started)
        except Exception:
            continue
    return max(ages) if ages else None


def game_is_running(compatdata):
    """True while the game itself (not just a leftover wineserver or a
    Wine service process) is alive inside this prefix."""
    return any(_is_game_process(p) for p in prefix_processes(compatdata))


def live_proton_for_prefix(compatdata, catalog):
    """The Proton build whose wine binaries are actually serving this
    prefix right now — read from the running processes themselves.
    Companion apps MUST use this build: mixing Proton versions in one
    prefix makes wine refuse with 'version mismatch' errors."""
    for p in prefix_processes(compatdata):
        exe = p["exe"]
        marker = "/files/bin/"
        if marker in exe:
            root = exe.split(marker)[0]
            for t in catalog:
                if str(Path(t["path"]).parent) == root:
                    return t
            proton_file = Path(root) / "proton"
            if proton_file.is_file():
                return {"name": Path(root).name + " (running)", "internal": "",
                        "path": str(proton_file), "kind": "live"}
    return None


def kill_prefix_wineservers(compatdata, log):
    """MANUAL cleanup only — kills every wineserver serving this
    prefix. Never called automatically: while a game boots there is
    a window where its wineserver is the only process visible, and
    killing it then kills the game itself. The user triggers this
    from the GUI when the game is fully closed and a leftover
    session (e.g. from another Proton build) is blocking launches."""
    killed = 0
    for p in prefix_processes(compatdata):
        if "wineserver" in p["cmdline"]:
            try:
                os.kill(p["pid"], 15)
                killed += 1
                log(f"Killed wineserver (pid {p['pid']}).")
            except Exception:
                pass
    if killed:
        time.sleep(1)
    return killed


def steam_launch_cmd(game):
    root = game.get("steam_root", "")
    if "com.valvesoftware.Steam" in root and shutil.which("flatpak"):
        return ["flatpak", "run", "com.valvesoftware.Steam",
                "-applaunch", game["appid"]]
    if "/snap/steam/" in root:
        snap_steam = Path("/snap/bin/steam")
        if snap_steam.exists():
            return [str(snap_steam), "-applaunch", game["appid"]]
        if shutil.which("snap"):
            return ["snap", "run", "steam", "-applaunch", game["appid"]]
    return ["steam", "-applaunch", game["appid"]]


def build_env(game):
    env = os.environ.copy()
    # Never hand a launcher-injected library path to Steam/Wine: when
    # Buddy runs from an AppImage (or any wrapper) the runtime's
    # LD_LIBRARY_PATH points into its own bundle, and Wine loading
    # foreign libraries makes the game and the companion apps land in
    # mismatched sessions — only one of them works at a time.
    for var in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH"):
        env.pop(var, None)
    env.update({
        "STEAM_COMPAT_DATA_PATH": game["compatdata"],
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": game["steam_root"],
        "STEAM_COMPAT_APP_ID": game["appid"],
    })
    pfx = Path(game["compatdata"]) / "pfx"
    if pfx.is_dir():
        env["WINEPREFIX"] = str(pfx)
    return env


def launch_app(game, proton, app, env, log):
    path = app["path"]
    args = shlex.split(app.get("args", "") or "")
    # Launch with the Proton build's own wine binary, NOT `proton run`.
    # `proton run` re-runs Proton's whole prefix-setup routine, and a
    # second setup fighting the game's live session can close the game.
    # A plain wine client simply joins the prefix's running wineserver
    # session — the same method protontricks-launch and
    # SteamTinkerLaunch use for companion apps.
    proton_root = Path(proton["path"]).parent
    wine_bin = next((p for p in (proton_root / "files" / "bin" / "wine64",
                                 proton_root / "files" / "bin" / "wine")
                     if p.is_file()), None)
    if wine_bin:
        log(f"  -> launching {Path(path).name} (via {wine_bin.name} of {proton['name']})")
        cmd = [str(wine_bin), path, *args]
    else:
        log(f"  -> launching {Path(path).name} (via proton run of {proton['name']})")
        cmd = [proton["path"], "run", path, *args]
    return subprocess.Popen(cmd, env=env, cwd=str(Path(path).parent))


def launch_apps(game, proton, apps, log, cancel, between_s=3, catalog=None):
    """Launch every companion app into the game's prefix.
    Returns (ok, [Popen ...]) — the handles let the babysitter close
    exactly the apps Buddy started, later."""
    # The apps must be served by the SAME wine build as everything else
    # in the prefix, or wine refuses with a version mismatch.
    if game_is_running(game["compatdata"]):
        live = live_proton_for_prefix(game["compatdata"], catalog or [])
        if live and live["path"] != proton["path"]:
            log(f"Note: the game is running under {live['name']}, so the apps "
                f"will use it too (not {proton['name']}) — versions must match.")
            proton = live
    elif prefix_processes(game["compatdata"]):
        # Never kill anything automatically: while a game boots there
        # is a window where its wineserver is the only process visible,
        # and killing it kills the game. A genuinely stale server is
        # ended only by the user (GUI button), never from here.
        log("Note: this prefix already has a Wine session "
            "(the game may still be starting, or an app is open).")
    env = build_env(game)
    popens = []
    for i, app in enumerate(apps):
        if cancel.is_set():
            log("Cancelled.")
            return False, popens
        popens.append(launch_app(game, proton, app, env, log))
        if i < len(apps) - 1:
            time.sleep(between_s)
    log("All companion apps launched into the game's prefix.")
    return True, popens


def terminate_processes(popens, log, grace_s=6):
    """Politely close processes Buddy started, then force any that
    ignore the polite request. Only ever aimed at Buddy's own app
    handles — never at the game, never at a wineserver."""
    alive = [p for p in popens if p.poll() is None]
    for p in alive:
        try:
            p.terminate()
        except Exception:
            pass
    deadline = time.time() + grace_s
    for p in alive:
        try:
            p.wait(timeout=max(0.0, deadline - time.time()))
        except Exception:
            pass
    for p in alive:
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
    return len(alive)


def watch_game_session(game, popens, log, stop_event, poll_s=5, confirm_misses=3):
    """Babysitter: after a successful launch, watch the game. When it
    has truly exited (several consecutive checks — a single flicker
    must not close anything), close the companion apps Buddy started,
    leaving the prefix clean for the next launch."""
    log("Babysitter on: when the game exits, its companion apps will be closed too.")
    misses = 0
    while not stop_event.is_set():
        if game_is_running(game["compatdata"]):
            misses = 0
        else:
            misses += 1
            if misses >= confirm_misses:
                log("Game exited — closing the companion apps.")
                n = terminate_processes(popens, log)
                log(f"Babysitter done: closed {n} app process(es). Prefix left clean.")
                return True
        if stop_event.wait(poll_s):
            break
    return False


def auto_launch(game, proton, apps, log, cancel, catalog=None,
                wait_timeout=240, settle_s=25, between_s=3):
    """Full auto flow: start the game via Steam, wait until it is really
    running in its prefix, then launch every companion app into it.
    Returns (ok, [Popen ...]) — the app handles, for the babysitter."""
    log(f"Game:  {game['name']} (App ID {game['appid']})")
    log(f"Proton: {proton['name']}  [{proton['path']}]")
    if not game_is_running(game["compatdata"]) and prefix_processes(game["compatdata"]):
        # A foreign Wine session (an app left open in this prefix)
        # already holds it, and Steam cannot start the game into an
        # occupied prefix — it flashes and dies. Refuse loudly here
        # instead of producing that failure.
        log("Blocked: this prefix already has a Wine session, but the "
            "game is not running in it — an app is holding the prefix "
            "open. Steam can't start the game until the prefix is "
            "free. Close the app (or end the session) first.")
        return False, []
    if game_is_running(game["compatdata"]):
        log("Game is already running in its prefix — skipping game launch.")
    else:
        log("Starting the game through Steam...")
        subprocess.Popen(steam_launch_cmd(game))
        log(f"Waiting for the game to start (up to {wait_timeout}s)...")
        start = time.time()
        while time.time() - start < wait_timeout:
            if cancel.is_set():
                log("Cancelled while waiting for the game.")
                return False, []
            if game_is_running(game["compatdata"]):
                break
            time.sleep(2)
        else:
            log("Timed out: the game never appeared in its prefix. "
                "Apps were NOT launched. Launch the game once in Steam "
                "(first run creates the prefix), then try again.")
            return False, []
        log("Game detected. Waiting until it is fully started "
            f"(about {settle_s}s in) before launching any apps...")
        deadline = time.time() + 180
        first_seen = time.time()
        last_alive = time.time()
        while time.time() < deadline:
            if cancel.is_set():
                log("Cancelled.")
                return False, []
            age = game_process_age(game["compatdata"])
            if age is not None:
                last_alive = time.time()
                eff_age = age
            elif game_is_running(game["compatdata"]):
                # Alive, but its process age could not be read this
                # poll — fall back to how long we have been watching it.
                last_alive = time.time()
                eff_age = time.time() - first_seen
            else:
                # Proton churns helper processes while a game starts,
                # so the game can vanish from the process list for a
                # few seconds without having closed. Only believe it
                # is gone after a sustained absence.
                if time.time() - last_alive > 20:
                    log("The game closed while it was starting — apps "
                        "were NOT launched.")
                    return False, []
                time.sleep(2)
                continue
            if eff_age >= settle_s:
                log(f"Game has been up for ~{int(eff_age)}s. Launching apps.")
                break
            time.sleep(2)
        else:
            log("The game is taking unusually long to start — apps "
                "were NOT launched. Start them with 'Apps only' once "
                "you are in the game.")
            return False, []
    return launch_apps(game, proton, apps, log, cancel, between_s, catalog)


# ------------------------------------------------------- Shortcuts

def _desktop_dir():
    try:
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True,
                             text=True, timeout=5)
        p = out.stdout.strip()
        if p:
            return Path(p)
    except Exception:
        pass
    return Path.home() / "Desktop"


def _write_desktop_file(path, name, exec_cmd, comment):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        f"Name={name}\n"
        f"Comment={comment}\n"
        f"Exec={exec_cmd}\n"
        "Icon=applications-games\n"
        "Terminal=false\n"
        "Categories=Game;\n")
    try:
        path.chmod(0o755)  # KDE/GNOME only launch desktop files that are executable
    except Exception:
        pass


def script_command():
    """How to run this very program — shortcuts point at whatever the
    user is actually running: the AppImage file itself when running
    from one (its mount path vanishes on exit, so never point at the
    script inside), otherwise the plain script with the system python."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return f'"{appimage}"'
    return f'python3 "{Path(__file__).resolve()}"'


def create_game_shortcut(game, log):
    """A double-clickable launcher: runs Buddy headless
    (--launch APPID) for this game and its saved companion apps.
    Placed on the Desktop and in the application menu."""
    exec_cmd = f"{script_command()} --launch {game['appid']}"
    comment = "Launch game + companion apps with Proton Buddy"
    fname = f"proton-buddy-{game['appid']}.desktop"
    made = []
    for folder in (Path.home() / ".local/share/applications", _desktop_dir()):
        target = folder / fname
        _write_desktop_file(target, f"{game['name']} + Apps", exec_cmd, comment)
        made.append(str(target))
    log(f"Shortcut created for {game['name']}: " + " and ".join(made))
    return made


def create_self_shortcut(log):
    """Put Proton Buddy itself in the application menu."""
    target = Path.home() / ".local/share/applications" / "proton-buddy.desktop"
    _write_desktop_file(target, "Proton Buddy", script_command(),
                        "Launch Steam games with companion apps in their Proton prefix")
    log(f"Proton Buddy added to the application menu: {target}")
    return target


# ------------------------------------------------------------ Saves

def save_candidates(game):
    """Folders inside the game's prefix that look like save storage:
    name-matching folders under the usual Windows save locations
    first, then every other folder found there."""
    pfx = Path(game["compatdata"]) / "pfx"
    if not pfx.is_dir():
        return []
    bases = []
    users = pfx / "drive_c" / "users"
    if users.is_dir():
        for user in sorted(users.iterdir()):
            if user.is_dir():
                bases += [user / "AppData" / "Roaming",
                          user / "AppData" / "Local",
                          user / "AppData" / "LocalLow",
                          user / "Documents" / "My Games",
                          user / "Saved Games",
                          user / "Documents"]
    gname = _norm(game["name"])
    scored = {}
    for base in bases:
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir():
                n = _norm(child.name)
                hit = bool(n and gname and (n in gname or gname in n))
                scored[child] = 2 if hit else max(scored.get(child, 0), 1)
    return sorted(scored, key=lambda p: (-scored[p], str(p)))


def backup_saves(game, save_path, log):
    """Copy the game's save folder into
    ~/.config/proton-buddy/backups/<appid>/<folder>_<timestamp>/."""
    save_path = Path(save_path)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / game["appid"] / f"{save_path.name}_{stamp}"
    n = 2
    while dest.exists():  # two backups within one second must not collide
        dest = BACKUP_DIR / game["appid"] / f"{save_path.name}_{stamp}_{n}"
        n += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(save_path, dest)
    log(f"Save backup saved: {dest} (from {save_path})")
    return dest


def latest_backup(game):
    root = BACKUP_DIR / game["appid"]
    if not root.is_dir():
        return None
    subs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    return subs[-1] if subs else None


def restore_saves(game, save_path, backup, log):
    """Replace the live save folder with a backup. The current saves
    are backed up first, so a restore is itself undoable."""
    backup_saves(game, save_path, log)  # safety snapshot of the present state
    save_path = Path(save_path)
    if save_path.exists():
        shutil.rmtree(save_path)
    shutil.copytree(backup, save_path)
    log(f"Restored saves from {backup} -> {save_path}")


# ------------------------------------------------------------------- GUI

# ---------------------------------------------------------------- Web UI
#
# The interface is a local web app (HTML/CSS/JS in WEB_HTML) served by
# a tiny stdlib HTTP server on 127.0.0.1 and opened in the user's
# browser. Pure standard library — no new dependencies — while the
# launch engine above stays exactly the same code the CLI uses.

WEB_HTML = "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>Proton Buddy</title>\n<style>\n:root{\n  color-scheme:dark;\n  --bg:#050607; --panel:#0b0d10; --edge:#262b33; --field:#0a0c0f;\n  --ink:#e8ecf1; --muted:#8b95a3; --red:#c8102e; --redb:#ff2438; --ember:#ff7a1a;\n  --chamfer:polygon(14px 0,100% 0,100% calc(100% - 14px),calc(100% - 14px) 100%,0 100%,0 14px);\n  --chamfer-s:polygon(9px 0,100% 0,100% calc(100% - 9px),calc(100% - 9px) 100%,0 100%,0 9px);\n}\n*{box-sizing:border-box}\nhtml,body{margin:0;padding:0}\nbody{\n  background:var(--bg); color:var(--ink);\n  font-family:\"Segoe UI\",system-ui,\"Noto Sans\",\"DejaVu Sans\",sans-serif;\n  min-height:100vh;\n  background-image:\n    radial-gradient(700px 320px at 50% -60px, rgba(200,16,46,.15), rgba(200,16,46,0) 70%),\n    linear-gradient(rgba(140,150,165,.05) 1px, transparent 1px),\n    linear-gradient(90deg, rgba(140,150,165,.05) 1px, transparent 1px),\n    url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='84' height='84'%3E%3Cpath d='M42 27 L57 42 L42 57 L27 42 Z' fill='none' stroke='%23221016' stroke-width='1.4'/%3E%3Ccircle cx='42' cy='42' r='2' fill='%232c1218'/%3E%3C/svg%3E\");\n  background-size:auto,28px 28px,28px 28px,84px 84px;\n}\nheader{max-width:1240px;margin:0 auto;padding:26px 22px 8px;display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}\n.brand{font-size:31px;font-weight:800;letter-spacing:.14em;line-height:1;\n  background:linear-gradient(90deg,var(--redb),var(--ember));\n  -webkit-background-clip:text;background-clip:text;color:transparent}\n.brandbar{width:190px;height:3px;background:var(--red);margin-top:9px}\n.tagline{color:var(--muted);font-size:13px;margin-top:9px}\n.pill{border:1px solid var(--edge);color:var(--muted);padding:8px 14px;font-size:11px;letter-spacing:.14em;background:rgba(11,13,16,.7)}\n.pill b{color:var(--ink)}\nmain{max-width:1240px;margin:14px auto 0;padding:0 22px;display:grid;grid-template-columns:minmax(0,1.55fr) minmax(0,1fr);gap:18px}\n@media (max-width:920px){ main{grid-template-columns:1fr} }\n.col{display:flex;flex-direction:column;gap:18px}\n.card{background:linear-gradient(180deg,#2b313a,#1b1f26);clip-path:var(--chamfer);padding:1px}\n.card-in{background:var(--panel);clip-path:var(--chamfer);padding:16px 18px;height:100%}\n.card h2{margin:0 0 12px;font-size:11px;letter-spacing:.2em;color:var(--ink);display:flex;align-items:center;gap:9px;text-transform:uppercase}\n.card h2::before{content:\"\";width:7px;height:7px;background:var(--red);flex:none}\n.card h2 .hint{margin-left:auto;color:var(--muted);letter-spacing:.04em;text-transform:none;font-size:11px}\n.ghead,.grow{display:grid;grid-template-columns:12px minmax(0,1fr) 74px 66px minmax(0,1.05fr);gap:10px;align-items:center}\n.ghead{color:var(--muted);font-size:10px;letter-spacing:.16em;padding:0 10px 7px}\n.grow{padding:9px 10px;cursor:pointer;border-top:1px solid #161a20}\n.grow:hover{background:#10131a}\n.grow.sel{background:linear-gradient(90deg,rgba(200,16,46,.17),rgba(200,16,46,0) 75%);box-shadow:inset 3px 0 0 var(--red)}\n.grow .nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n.grow .sub{color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n.dot{width:8px;height:8px;border-radius:50%;background:#2c313a}\n.dot.on{background:#37d67a;box-shadow:0 0 8px #37d67a;animation:pulse 1.6s infinite}\n@keyframes pulse{50%{opacity:.45}}\n.games-wrap{max-height:430px;overflow:auto}\ninput[type=text]{width:100%;background:var(--field);color:var(--ink);border:1px solid #2a2f38;padding:10px 11px;font-size:13px;outline:none}\ninput[type=text]:focus{border-color:var(--red)}\nselect{appearance:none;-webkit-appearance:none;width:100%;color:var(--ink);border:1px solid #2a2f38;padding:10px 34px 10px 11px;font-size:13px;outline:none;cursor:pointer;background:var(--field) url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%23ff2438' stroke-width='1.6' fill='none'/%3E%3C/svg%3E\") no-repeat right 12px center}\nselect:focus{border-color:var(--red)}\noption{background:#0d0f12;color:var(--ink)}\n.dd{position:relative}\n.dd-bar{display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--field);border:1px solid #2a2f38;padding:10px 12px;font-size:13px;cursor:pointer;user-select:none}\n.dd-bar:hover{border-color:#3a4150}\n.dd-chev{width:8px;height:8px;flex:none;border-right:2px solid var(--redb);border-bottom:2px solid var(--redb);transform:rotate(45deg) translateY(-2px)}\n.dd-list{margin-top:4px;background:#0d0f12;border:1px solid #2a2f38;max-height:264px;overflow:auto}\n.dd-opt{padding:10px 12px;cursor:pointer;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n.dd-opt:hover{background:#141920}\n.dd-opt.sel{background:rgba(200,16,46,.16);box-shadow:inset 3px 0 0 var(--red)}\n.note{color:var(--muted);font-size:12px;margin-top:8px;word-break:break-all}\n.appitem{padding:8px 10px;border-top:1px solid #161a20;cursor:pointer;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n.appitem:first-child{border-top:none}\n.appitem:hover{background:#10131a}\n.appitem.sel{background:rgba(200,16,46,.15);box-shadow:inset 3px 0 0 var(--red)}\n.prow{display:flex;justify-content:space-between;align-items:baseline;gap:14px;padding:6px 10px;border-top:1px solid #161a20;cursor:pointer}\n.prow:first-child{border-top:none}\n.prow:hover{background:#10131a}\n.prow.sel{background:rgba(200,16,46,.15);box-shadow:inset 3px 0 0 var(--red)}\n.prow .nm{font-size:13px;font-weight:600;white-space:nowrap}\n.prow .sub{color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;text-align:right}\n.apps-wrap{border:1px solid #1d2129;background:var(--field);max-height:132px;overflow:auto}\n.minirow{display:flex;gap:8px;margin-top:10px;align-items:center}\n.minirow input{flex:1}\n.btn{appearance:none;border:none;cursor:pointer;color:var(--ink);background:#101216;\n  box-shadow:inset 0 0 0 1px #3a4150;padding:11px 18px;font:700 11px/1 \"Segoe UI\",system-ui,sans-serif;\n  letter-spacing:.14em;text-transform:uppercase;clip-path:var(--chamfer-s);transition:background .15s,box-shadow .15s}\n.btn:hover{box-shadow:inset 0 0 0 1px var(--redb);background:#15181e;color:#fff}\n.btn:active{transform:translateY(1px)}\n.btn.accent{background:linear-gradient(90deg,var(--redb),var(--red) 58%,#8f0f22);color:#fff;box-shadow:none}\n.btn.accent:hover{background:linear-gradient(90deg,#ff3049,#d40f27 58%,#a30d24)}\n.btn.big{padding:16px 26px;font-size:12px}\n.btn.small{padding:9px 13px;font-size:10px}\n.btn[disabled]{opacity:.4;pointer-events:none}\n.switchrow{display:flex;align-items:center;gap:11px;margin-top:13px;cursor:pointer;user-select:none}\n.switch{position:relative;width:42px;height:22px;flex:none}\n.switch input{opacity:0;width:0;height:0}\n.track{position:absolute;inset:0;background:#1a1e24;box-shadow:inset 0 0 0 1px #3a4150;transition:.15s}\n.track::before{content:\"\";position:absolute;width:14px;height:14px;left:4px;top:4px;background:#8b95a3;transition:.15s}\n.switch input:checked + .track{background:rgba(200,16,46,.35);box-shadow:inset 0 0 0 1px var(--red)}\n.switch input:checked + .track::before{transform:translateX(19px);background:var(--redb)}\n.switchlbl{font-size:13px}\n.actions{max-width:1240px;margin:18px auto 0;padding:0 22px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}\n.statusline{color:var(--muted);font-size:12px;margin-left:6px}\n.logcard{max-width:1240px;margin:18px auto 26px;padding:0 22px}\n.logbox{background:#07080a;border:1px solid #1d2129;height:190px;overflow:auto;padding:11px 13px;\n  font-family:ui-monospace,\"Cascadia Mono\",Consolas,\"DejaVu Sans Mono\",monospace;font-size:12px;line-height:1.6;color:#c3cad4}\n.logbox .err{color:#ff7183}\n.logbox .ok{color:#7ddba3}\n::-webkit-scrollbar{width:9px;height:9px}\n::-webkit-scrollbar-thumb{background:#23272f}\n::-webkit-scrollbar-track{background:transparent}\n.overlay{position:fixed;inset:0;background:rgba(2,3,4,.72);display:none;align-items:center;justify-content:center;z-index:60;padding:18px}\n.overlay.show{display:flex}\n.modal{width:min(580px,94vw)}\n.modal .card-in{padding:20px 22px}\n.modal h3{margin:0 0 10px;font-size:14px;letter-spacing:.08em}\n.modal p{color:var(--muted);font-size:13px;line-height:1.55;white-space:pre-wrap;word-break:break-word}\n.modal .btnrow{display:flex;gap:9px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap}\n.pickitem{padding:9px 11px;border-top:1px solid #161a20;cursor:pointer;font-size:12.5px;word-break:break-all}\n.pickitem:hover{background:#10131a}\n.pickitem.sel{background:rgba(200,16,46,.15);box-shadow:inset 3px 0 0 var(--red)}\n.crumbs{color:var(--muted);font-size:12px;margin-bottom:9px;word-break:break-all}\n.crumbs b{color:var(--ink)}\n.frow{display:flex;gap:9px;padding:7px 9px;cursor:pointer;font-size:13px;align-items:center}\n.frow:hover{background:#10131a}\n.frow.sel{background:rgba(200,16,46,.15);box-shadow:inset 3px 0 0 var(--red)}\n.frow .ic{width:18px;text-align:center;flex:none}\n.browse-wrap{max-height:300px;overflow:auto;border:1px solid #1d2129;background:var(--field)}\n#toasts{position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;gap:9px;z-index:80}\n.toast{background:#101216;border:1px solid var(--edge);border-left:3px solid var(--red);color:var(--ink);\n  padding:11px 15px;font-size:13px;max-width:380px;box-shadow:0 8px 28px rgba(0,0,0,.5)}\n</style>\n</head>\n<body>\n<header>\n  <div>\n    <div class=\"brand\">PROTON BUDDY</div>\n    <div class=\"brandbar\"></div>\n    <div class=\"tagline\">Pick a game · add its apps · one click launches everything in the same Proton prefix · <span style=\"color:var(--redb)\">v__PB_VERSION__</span></div>\n  </div>\n  <div class=\"pill\" id=\"counts\">LOADING…</div>\n</header>\n\n<main>\n  <section class=\"card\"><div class=\"card-in\">\n    <h2>1 · Installed Steam games <span class=\"hint\">all libraries · all disks</span></h2>\n    <div class=\"ghead\"><span></span><span>GAME</span><span>APP ID</span><span>PREFIX</span><span>PROTON (AUTO)</span></div>\n    <div class=\"games-wrap\" id=\"games\"></div>\n    <div class=\"minirow\" style=\"justify-content:flex-end\">\n      <button class=\"btn small\" id=\"btn-rescan\">Rescan Steam libraries</button>\n    </div>\n  </div></section>\n\n  <div class=\"col\">\n    <section class=\"card\"><div class=\"card-in\">\n      <h2>2 · Proton runtime</h2>\n      <div class=\"apps-wrap\" id=\"proton-rows\" style=\"max-height:196px\"></div>\n      <div class=\"note\" id=\"proton-note\"></div>\n    </div></section>\n\n    <section class=\"card\"><div class=\"card-in\">\n      <h2>3 · Companion apps</h2>\n      <div class=\"apps-wrap\" id=\"apps\"></div>\n      <div class=\"minirow\">\n        <button class=\"btn small\" id=\"btn-add-app\">Add EXE…</button>\n        <button class=\"btn small\" id=\"btn-del-app\">Remove</button>\n      </div>\n      <div class=\"minirow\">\n        <input type=\"text\" id=\"paste-path\" placeholder=\"…or paste a full path to an .exe\">\n        <button class=\"btn small\" id=\"btn-paste-add\">Add</button>\n      </div>\n      <div class=\"minirow\">\n        <input type=\"text\" id=\"app-args\" placeholder=\"Arguments for selected app\">\n      </div>\n      <label class=\"switchrow\">\n        <span class=\"switch\"><input type=\"checkbox\" id=\"babysit\"><span class=\"track\"></span></span>\n        <span class=\"switchlbl\">Babysitter — close the companion apps when the game exits</span>\n      </label>\n    </div></section>\n\n    <section class=\"card\"><div class=\"card-in\">\n      <h2>4 · Shortcuts &amp; saves</h2>\n      <div class=\"minirow\" style=\"flex-wrap:wrap\">\n        <button class=\"btn small\" id=\"btn-shortcut\">Desktop shortcut</button>\n        <button class=\"btn small\" id=\"btn-backup\">Back up saves</button>\n        <button class=\"btn small\" id=\"btn-restore\">Restore backup</button>\n        <button class=\"btn small\" id=\"btn-menu\">Add Buddy to app menu</button>\n      </div>\n    </div></section>\n  </div>\n</main>\n\n<div class=\"actions\">\n  <button class=\"btn big accent launchable\" id=\"btn-auto\">Launch game + apps (AUTO)</button>\n  <button class=\"btn big launchable\" id=\"btn-game\">Game only</button>\n  <button class=\"btn big launchable\" id=\"btn-apps\">Apps only</button>\n  <button class=\"btn big\" id=\"btn-cancel\" disabled>Cancel</button>\n  <button class=\"btn big launchable\" id=\"btn-end\">End leftover Wine session</button>\n  <span class=\"statusline\" id=\"statusline\"></span>\n</div>\n\n<div class=\"logcard\">\n  <section class=\"card\"><div class=\"card-in\">\n    <h2>Log</h2>\n    <div class=\"logbox\" id=\"log\"></div>\n  </div></section>\n</div>\n\n<div class=\"overlay\" id=\"overlay\"><div class=\"modal card\"><div class=\"card-in\" id=\"modal-body\"></div></div></div>\n<div id=\"toasts\"></div>\n\n<script>\n\"use strict\";\nlet S = null, sel = null, selApp = -1, lastLog = 0, version = -1, qId = null;\nlet browsePath = \"\", browseSel = null;\nconst $ = id => document.getElementById(id);\n\nasync function api(path, body){\n  const r = await fetch(path, body ? {method:\"POST\", headers:{\"Content-Type\":\"application/json\"}, body:JSON.stringify(body)} : {});\n  return r.json();\n}\nfunction toast(msg){\n  const t = document.createElement(\"div\"); t.className = \"toast\"; t.textContent = msg;\n  $(\"toasts\").appendChild(t); setTimeout(() => t.remove(), 5200);\n}\nfunction showModal(html){ $(\"modal-body\").innerHTML = html; $(\"overlay\").classList.add(\"show\"); }\nfunction hideModal(){ $(\"overlay\").classList.remove(\"show\"); }\nfunction confirmModal(title, text, okLabel){\n  return new Promise(res => {\n    showModal(`<h3>${title}</h3><p>${text}</p><div class=\"btnrow\">\n      <button class=\"btn\" id=\"m-cancel\">Cancel</button>\n      <button class=\"btn accent\" id=\"m-ok\">${okLabel || \"OK\"}</button></div>`);\n    $(\"m-cancel\").onclick = () => { hideModal(); res(false); };\n    $(\"m-ok\").onclick = () => { hideModal(); res(true); };\n  });\n}\nfunction pickModal(title, options){\n  return new Promise(res => {\n    let chosen = 0;\n    showModal(`<h3>${title}</h3><div class=\"apps-wrap\" style=\"max-height:260px\">` +\n      options.map((o,i) => `<div class=\"pickitem${i===0?\" sel\":\"\"}\" data-i=\"${i}\">${o}</div>`).join(\"\") +\n      `</div><div class=\"btnrow\"><button class=\"btn\" id=\"m-cancel\">Cancel</button>\n      <button class=\"btn accent\" id=\"m-ok\">Use this folder</button></div>`);\n    document.querySelectorAll(\".pickitem\").forEach(el => el.onclick = () => {\n      document.querySelectorAll(\".pickitem\").forEach(e => e.classList.remove(\"sel\"));\n      el.classList.add(\"sel\"); chosen = +el.dataset.i;\n    });\n    $(\"m-cancel\").onclick = () => { hideModal(); res(null); };\n    $(\"m-ok\").onclick = () => { hideModal(); res(options[chosen]); };\n  });\n}\nasync function browseModal(){\n  browseSel = null;\n  showModal(`<h3>Choose a companion .exe</h3><div class=\"crumbs\" id=\"crumbs\"></div>\n    <div class=\"browse-wrap\" id=\"browse\"></div>\n    <div class=\"btnrow\"><button class=\"btn\" id=\"m-home\">Home</button>\n    <button class=\"btn\" id=\"m-cancel\">Cancel</button>\n    <button class=\"btn accent\" id=\"m-ok\" disabled>Select</button></div>`);\n  $(\"m-cancel\").onclick = hideModal;\n  $(\"m-home\").onclick = () => loadBrowse(\"\");\n  $(\"m-ok\").onclick = async () => {\n    if(!browseSel) return;\n    const full = browsePath.replace(/\\/$/,\"\") + \"/\" + browseSel;\n    hideModal();\n    const r = await api(\"/api/apps/add\", {appid: sel, path: full});\n    if(r.error) toast(r.error); else { toast(\"Added: \" + full); version = -1; }\n  };\n  loadBrowse(\"\");\n}\nasync function loadBrowse(path){\n  const r = await api(\"/api/browse?path=\" + encodeURIComponent(path || \"\"));\n  browsePath = r.path; browseSel = null; $(\"m-ok\").disabled = true;\n  $(\"crumbs\").innerHTML = \"Location: <b>\" + r.path + \"</b>\";\n  let html = \"\";\n  if(r.parent) html += `<div class=\"frow\" data-dir=\"${r.parent}\"><span class=\"ic\">↩</span>.. (up)</div>`;\n  for(const d of r.dirs) html += `<div class=\"frow\" data-dir=\"${(r.path + \"/\" + d).replace(/\"/g,\"&quot;\")}\"><span class=\"ic\">📁</span>${d}</div>`;\n  for(const f of r.exes) html += `<div class=\"frow\" data-exe=\"${f.replace(/\"/g,\"&quot;\")}\"><span class=\"ic\">⚙️</span>${f}</div>`;\n  if(!r.dirs.length && !r.exes.length) html += `<div class=\"frow\">— empty —</div>`;\n  $(\"browse\").innerHTML = html;\n  document.querySelectorAll(\"#browse .frow\").forEach(el => el.onclick = () => {\n    if(el.dataset.dir !== undefined && el.dataset.dir !== \"\") loadBrowse(el.dataset.dir);\n    else if(el.dataset.exe){\n      document.querySelectorAll(\"#browse .frow\").forEach(e => e.classList.remove(\"sel\"));\n      el.classList.add(\"sel\"); browseSel = el.dataset.exe; $(\"m-ok\").disabled = false;\n    }\n  });\n}\nfunction esc(s){ const d = document.createElement(\"div\"); d.textContent = s; return d.innerHTML; }\n\nfunction renderAll(){\n  const games = S.games;\n  $(\"counts\").innerHTML = `<b>${S.libraries}</b>&nbsp;LIBRARIES&nbsp;&nbsp;·&nbsp;&nbsp;<b>${games.length}</b>&nbsp;GAMES&nbsp;&nbsp;·&nbsp;&nbsp;<b>${S.protons.length}</b>&nbsp;PROTON RUNTIMES`;\n  if(!games.find(g => g.appid === sel)) sel = (S.config.last_game && games.find(g => g.appid === S.config.last_game)) ? S.config.last_game : (games[0] || {}).appid;\n  $(\"games\").innerHTML = games.map(g => `\n    <div class=\"grow${g.appid===sel?\" sel\":\"\"}\" data-appid=\"${g.appid}\">\n      <span class=\"dot${g.running?\" on\":\"\"}\" id=\"dot-${g.appid}\"></span>\n      <span><div class=\"nm\">${esc(g.name)}</div><div class=\"sub\">${esc(g.library)}</div></span>\n      <span class=\"sub\">${g.appid}</span>\n      <span class=\"sub\">${g.has_prefix ? \"Yes\" : \"Not yet\"}</span>\n      <span class=\"sub\">${esc(g.proton || \"—\")}</span>\n    </div>`).join(\"\");\n  document.querySelectorAll(\".grow\").forEach(el => el.onclick = async () => {\n    sel = el.dataset.appid; selApp = -1;\n    await api(\"/api/select\", {appid: sel}); renderAll();\n  });\n  const g = games.find(x => x.appid === sel);\n  const ov = (S.config.overrides || {})[sel];\n  renderProtonRows(ov);\n  if(g){\n    $(\"proton-note\").textContent = ov ? \"Using your chosen Proton — \" + ov\n      : \"Auto: Steam's configured Proton for this game — \" + (g.proton_path || \"none found\");\n  }\n  const apps = (S.config.apps || {})[sel] || [];\n  if(selApp >= apps.length) selApp = apps.length - 1;\n  $(\"apps\").innerHTML = apps.length ? apps.map((a,i) =>\n    `<div class=\"appitem${i===selApp?\" sel\":\"\"}\" data-i=\"${i}\">${esc(a.path)}</div>`).join(\"\")\n    : `<div class=\"appitem\" style=\"cursor:default;color:var(--muted)\">No companion apps yet — add the trainer / tool .exe below.</div>`;\n  document.querySelectorAll(\".appitem[data-i]\").forEach(el => el.onclick = () => {\n    selApp = +el.dataset.i;\n    $(\"app-args\").value = apps[selApp].args || \"\";\n    renderAppsOnly();\n  });\n  $(\"app-args\").value = selApp >= 0 ? (apps[selApp].args || \"\") : \"\";\n  $(\"babysit\").checked = !!(((S.config.settings || {})[sel] || {}).close_apps_on_exit);\n}\nfunction renderAppsOnly(){\n  const apps = (S.config.apps || {})[sel] || [];\n  document.querySelectorAll(\".appitem[data-i]\").forEach(el =>\n    el.classList.toggle(\"sel\", +el.dataset.i === selApp));\n}\nfunction appendLogs(lines){\n  if(!lines.length) return;\n  const box = $(\"log\"), nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;\n  for(const l of lines){\n    const d = document.createElement(\"div\");\n    if(/Blocked|Error|Timed out|did not/.test(l.text)) d.className = \"err\";\n    if(/launched|Babysitter done|detected/.test(l.text)) d.className = \"ok\";\n    d.textContent = l.text; box.appendChild(d);\n    lastLog = Math.max(lastLog, l.id);\n    $(\"statusline\").textContent = l.text;\n  }\n  while(box.children.length > 400) box.removeChild(box.firstChild);\n  if(nearBottom) box.scrollTop = box.scrollHeight;\n}\nasync function poll(){\n  try{\n    const st = await (await fetch(\"/api/state?since=\" + lastLog)).json();\n    S = st;\n    appendLogs(st.logs || []);\n    document.querySelectorAll(\".launchable\").forEach(b => b.disabled = st.busy);\n    $(\"btn-cancel\").disabled = !st.busy;\n    for(const g of st.games){\n      const dot = $(\"dot-\" + g.appid);\n      if(dot) dot.className = \"dot\" + (g.running ? \" on\" : \"\");\n    }\n    if(st.question && qId !== st.question.id){\n      qId = st.question.id;\n      const ok = await confirmModal(st.question.title, st.question.text, \"End it and continue\");\n      await api(\"/api/answer\", {ok});\n    }\n    if(!st.question) qId = null;\n    if(st.version !== version){ version = st.version; renderAll(); }\n  }catch(e){ /* server momentarily unreachable */ }\n  setTimeout(poll, 1000);\n}\n\n$(\"btn-rescan\").onclick = async () => { await api(\"/api/rescan\", {}); version = -1; toast(\"Libraries rescanned.\"); };\nfunction renderProtonRows(ov){\n  const g = S.games.find(x => x.appid === sel);\n  const opts = [{path:\"\", name:\"Auto (recommended)\", sub:\"per-game setting\"}].concat(\n    S.protons.map(pr => ({path: pr.path, name: pr.name, sub: pr.path})));\n  $(\"proton-rows\").innerHTML = opts.map(o => {\n    const isSel = o.path === (ov || \"\");\n    const tag = (g && o.path && g.proton_path === o.path)\n      ? ' <span style=\"color:var(--redb);font-size:10px;letter-spacing:.1em\">· STEAM\\'S PICK</span>' : \"\";\n    return `<div class=\"prow${isSel ? \" sel\" : \"\"}\" data-path=\"${esc(o.path)}\"><span class=\"nm\">${esc(o.name)}${tag}</span><span class=\"sub\">${esc(o.sub)}</span></div>`;\n  }).join(\"\");\n  $(\"proton-rows\").querySelectorAll(\".prow\").forEach(el => el.onclick = async () => {\n    await api(\"/api/proton\", {appid: sel, path: el.dataset.path});\n    version = -1;\n    toast(el.dataset.path ? \"Proton override saved.\" : \"Proton set back to Auto.\");\n  });\n}\n$(\"btn-add-app\").onclick = () => { if(!sel) return toast(\"Select a game first.\"); browseModal(); };\n$(\"btn-paste-add\").onclick = async () => {\n  const p = $(\"paste-path\").value.trim(); if(!p) return;\n  const r = await api(\"/api/apps/add\", {appid: sel, path: p});\n  if(r.error) toast(r.error); else { $(\"paste-path\").value = \"\"; version = -1; toast(\"Added: \" + p); }\n};\n$(\"btn-del-app\").onclick = async () => {\n  if(selApp < 0) return toast(\"Select an app in the list first.\");\n  await api(\"/api/apps/remove\", {appid: sel, index: selApp}); selApp = -1; version = -1;\n};\n$(\"app-args\").onchange = async () => {\n  if(selApp < 0) return;\n  await api(\"/api/apps/args\", {appid: sel, index: selApp, args: $(\"app-args\").value.trim()});\n};\n$(\"babysit\").onchange = async () => {\n  await api(\"/api/settings\", {appid: sel, close_apps_on_exit: $(\"babysit\").checked});\n  toast(\"Babysitter \" + ($(\"babysit\").checked ? \"ON\" : \"OFF\") + \" for the selected game.\");\n};\nasync function launch(mode){\n  if(!sel) return toast(\"Select a game first.\");\n  let r = await api(\"/api/launch\", {appid: sel, mode});\n  if(r.needs_confirm){\n    const ok = await confirmModal(\"The game is not running\",\n      \"If an app starts first, it grabs the game's Proton prefix, and Steam may then refuse to start the game until you close the app.\\n\\nRecommended: AUTO — it waits for the game before starting the apps.\\n\\nLaunch the apps anyway?\", \"Launch anyway\");\n    if(!ok) return;\n    r = await api(\"/api/launch\", {appid: sel, mode, confirm: true});\n  }\n  if(r.error) toast(r.error);\n}\n$(\"btn-auto\").onclick = () => launch(\"auto\");\n$(\"btn-game\").onclick = () => launch(\"game\");\n$(\"btn-apps\").onclick = () => launch(\"apps\");\n$(\"btn-cancel\").onclick = async () => { await api(\"/api/cancel\", {}); };\n$(\"btn-end\").onclick = async () => {\n  const ok = await confirmModal(\"End leftover Wine session?\",\n    \"This kills the wineserver still holding the selected game's prefix (for example after a crash, or one started by another Proton build). Any companion app still open in this prefix will close too.\\n\\nThe game itself must be fully closed.\", \"End session\");\n  if(!ok) return;\n  const r = await api(\"/api/end-session\", {appid: sel});\n  toast(r.error ? r.error : \"Leftover session ended.\");\n};\n$(\"btn-shortcut\").onclick = async () => {\n  const r = await api(\"/api/shortcut\", {appid: sel});\n  toast(r.error ? r.error : \"Shortcut created on the Desktop and in the app menu.\");\n};\n$(\"btn-menu\").onclick = async () => {\n  const r = await api(\"/api/install-self\", {});\n  toast(r.error ? r.error : \"Proton Buddy is now in your application menu.\");\n};\nasync function saveFlow(kind){\n  let r = await api(\"/api/\" + kind, {appid: sel});\n  if(r.need_pick){\n    const chosen = await pickModal(kind === \"backup\" ? \"Which folder holds the saves?\" : \"Restore into which folder?\", r.need_pick);\n    if(!chosen) return;\n    r = await api(\"/api/\" + kind, {appid: sel, save_path: chosen});\n  }\n  if(r.error) toast(r.error);\n  else toast(kind === \"backup\" ? \"Backup saved: \" + r.dest : \"Saves restored.\");\n}\n$(\"btn-backup\").onclick = () => saveFlow(\"backup\");\n$(\"btn-restore\").onclick = async () => {\n  const ok = await confirmModal(\"Restore latest backup?\",\n    \"Your current saves are backed up first, so this can be undone. The game must be closed.\", \"Restore\");\n  if(ok) saveFlow(\"restore\");\n};\npoll();\n</script>\n</body>\n</html>\n"


def browse_dir(path_str):
    """One level of a folder, for the web UI's .exe picker."""
    p = Path(path_str).expanduser() if path_str else Path.home()
    if not p.is_dir():
        p = Path.home()
    dirs, exes = [], []
    try:
        children = sorted(p.iterdir(), key=lambda c: c.name.casefold())
    except Exception:
        children = []
    for child in children:
        try:
            if child.is_dir() and not child.name.startswith("."):
                dirs.append(child.name)
            elif child.is_file() and child.suffix.lower() == ".exe":
                exes.append(child.name)
        except Exception:
            pass
    return {"path": str(p),
            "parent": str(p.parent) if str(p) != "/" else None,
            "dirs": dirs, "exes": exes, "home": str(Path.home())}


class BuddyWebState:
    """Everything the web UI needs: catalog, config, job control,
    a log buffer the page polls, and a question channel for the one
    decision a launch can need mid-flight (ending an occupied prefix)."""

    def __init__(self):
        self.cfg = read_config()
        self.roots = steam_roots()
        self.lib_owner = all_libraries(self.roots)
        self.mapping = {}
        for r in self.roots:
            self.mapping.update(parse_compat_tool_mapping(r))
        self.games = []
        self.catalog = []
        self.game_by_id = {}
        self.lock = threading.Lock()
        self.logs = []
        self.log_seq = 0
        self.busy = False
        self.cancel = threading.Event()
        self.question = None
        self._q_event = threading.Event()
        self._q_answer = False
        self.version = 0
        self.refresh()

    # ------------------------------------------------------------ data
    def refresh(self):
        self.games = discover_games(self.roots, self.lib_owner)
        self.catalog = discover_protons(self.roots, list(self.lib_owner.keys()))
        self.game_by_id = {g["appid"]: g for g in self.games}
        self.bump()

    def bump(self):
        self.version += 1

    def game(self, appid):
        return self.game_by_id.get(str(appid))

    def tool_for(self, game):
        override = self.cfg.get("proton_override", {}).get(game["appid"])
        if override:
            t = next((t for t in self.catalog if t["path"] == override), None)
            if t:
                return t
        t, _ = auto_proton_for(game["appid"], self.catalog, self.mapping)
        return t

    def log(self, msg):
        file_log(msg)
        with self.lock:
            self.log_seq += 1
            self.logs.append({"id": self.log_seq, "text": str(msg)})
            del self.logs[:-400]

    def public_state(self, since=0):
        with self.lock:
            logs = [l for l in self.logs if l["id"] > since]
        games = []
        for g in self.games:
            t = self.tool_for(g)
            games.append({
                "appid": g["appid"], "name": g["name"], "library": g["library"],
                "has_prefix": bool(g["prefix"]),
                "proton": t["name"] if t else None,
                "proton_path": t["path"] if t else None,
                "running": game_is_running(g["compatdata"]),
            })
        return {
            "version": self.version,
            "busy": self.busy,
            "question": self.question,
            "games": games,
            "protons": self.catalog,
            "libraries": len(self.lib_owner),
            "config": {
                "apps": self.cfg.get("apps", {}),
                "settings": self.cfg.get("game_settings", {}),
                "overrides": self.cfg.get("proton_override", {}),
                "last_game": self.cfg.get("last_game", ""),
            },
            "logs": logs,
            "log_seq": self.log_seq,
        }

    # ------------------------------------------------------ questions
    def ask(self, title, text, timeout=180):
        self.question = {"id": self.log_seq + 1, "title": title, "text": text}
        self._q_event.clear()
        self._q_answer = False
        self.log("Waiting for your answer in the browser window...")
        self._q_event.wait(timeout)
        self.question = None
        return self._q_answer

    def answer(self, ok):
        self._q_answer = bool(ok)
        self._q_event.set()

    # -------------------------------------------------------- actions
    def _babysit(self, game, popens):
        settings = cfg_game_settings(self.cfg, game["appid"])
        if popens and settings.get("close_apps_on_exit"):
            threading.Thread(target=watch_game_session,
                             args=(game, popens, self.log, threading.Event()),
                             daemon=True).start()

    def _start_job(self, fn):
        if self.busy:
            return False
        self.busy = True
        self.cancel = threading.Event()

        def runner():
            try:
                fn()
            except Exception as exc:
                self.log(f"Error: {exc}")
            finally:
                self.busy = False
        threading.Thread(target=runner, daemon=True).start()
        return True

    def launch(self, appid, mode, confirm=False):
        game = self.game(appid)
        if not game:
            return {"error": "Unknown game."}
        tool = self.tool_for(game)
        if not tool:
            return {"error": "No Proton runtime found on any disk."}
        apps = [dict(a) for a in cfg_apps(self.cfg, game["appid"])
                if Path(a["path"]).is_file()]
        if mode == "game":
            try:
                subprocess.Popen(steam_launch_cmd(game))
                self.log(f"Asked Steam to launch {game['name']}.")
                return {"ok": True}
            except Exception as exc:
                return {"error": str(exc)}
        if not apps:
            return {"error": "Add at least one companion .exe for this game first."}
        if mode == "apps":
            if not game["prefix"]:
                return {"error": "This game has no Proton prefix yet. "
                                 "Launch it once through Steam first."}
            if not game_is_running(game["compatdata"]) and not confirm:
                return {"needs_confirm": True}

            def fn_apps():
                self.log(f"Launching apps into {game['name']}'s prefix with {tool['name']}...")
                ok, popens = launch_apps(game, tool, apps, self.log, self.cancel,
                                         catalog=self.catalog)
                if ok:
                    self._babysit(game, popens)
            if not self._start_job(fn_apps):
                return {"error": "Busy — a launch is already in progress."}
            return {"ok": True}

        def fn_auto():
            # Pre-phase, same as the old GUI: wait out a possible boot,
            # then ask before ending an occupied prefix.
            if (prefix_processes(game["compatdata"])
                    and not game_is_running(game["compatdata"])):
                self.log("A Wine session already exists in this game's "
                         "prefix — waiting to see if the game is still starting...")
                for _ in range(15):
                    if self.cancel.is_set():
                        self.log("Cancelled.")
                        return
                    if game_is_running(game["compatdata"]):
                        break
                    time.sleep(2)
            if (prefix_processes(game["compatdata"])
                    and not game_is_running(game["compatdata"])):
                self.log("That session still hasn't produced the game — "
                         "most likely an app is holding the prefix, and "
                         "Steam can't start the game into it.")
                if not self.ask(
                        "Prefix is occupied",
                        "A Wine session is open in this game's Proton prefix, "
                        "but the game hasn't appeared in it — an app is holding "
                        "the prefix (or a launch is stuck).\n\nSteam cannot "
                        "start the game while this session lasts. End it now "
                        "(whatever is holding the prefix will close) and "
                        "continue with AUTO?"):
                    self.log("Stopped — close the app holding the prefix, "
                             "then run AUTO again.")
                    return
                kill_prefix_wineservers(game["compatdata"], self.log)
                time.sleep(1)
            ok, popens = auto_launch(game, tool, apps, self.log, self.cancel,
                                     catalog=self.catalog)
            if ok:
                self._babysit(game, popens)
        if not self._start_job(fn_auto):
            return {"error": "Busy — a launch is already in progress."}
        return {"ok": True}

    def end_session(self, appid):
        game = self.game(appid)
        if not game:
            return {"error": "Unknown game."}
        if game_is_running(game["compatdata"]):
            return {"error": "The game is running in this prefix. Close the "
                             "game first — ending the session would kill it."}
        if not prefix_processes(game["compatdata"]):
            return {"error": "Nothing is running in this game's prefix."}
        killed = kill_prefix_wineservers(game["compatdata"], self.log)
        if killed:
            self.log(f"Ended {killed} leftover wineserver(s) for {game['name']}.")
        return {"ok": True, "killed": killed}

    def resolve_save_path(self, game, chosen=None):
        settings = cfg_game_settings(self.cfg, game["appid"])
        if chosen:
            settings["save_path"] = str(chosen)
            save_config(self.cfg)
            return Path(chosen), None
        saved = settings.get("save_path")
        if saved and Path(saved).is_dir():
            return Path(saved), None
        cands = save_candidates(game)
        if not cands:
            return None, {"error": "No save folder found inside this game's "
                                   "prefix yet (the game may keep saves "
                                   "elsewhere, or hasn't saved yet)."}
        if len(cands) == 1:
            settings["save_path"] = str(cands[0])
            save_config(self.cfg)
            self.log(f"Save folder for {game['name']}: {cands[0]}")
            return cands[0], None
        return None, {"need_pick": [str(c) for c in cands]}

    def backup(self, appid, chosen=None):
        game = self.game(appid)
        if not game:
            return {"error": "Unknown game."}
        path, err = self.resolve_save_path(game, chosen)
        if err:
            return err
        try:
            dest = backup_saves(game, path, self.log)
            return {"ok": True, "dest": str(dest)}
        except Exception as exc:
            return {"error": str(exc)}

    def restore(self, appid, chosen=None):
        game = self.game(appid)
        if not game:
            return {"error": "Unknown game."}
        if game_is_running(game["compatdata"]):
            return {"error": "Close the game before restoring saves — "
                             "it would overwrite the restored files."}
        backup = latest_backup(game)
        if not backup:
            return {"error": "No backups for this game yet."}
        path, err = self.resolve_save_path(game, chosen)
        if err:
            return err
        try:
            restore_saves(game, path, backup, self.log)
            return {"ok": True}
        except Exception as exc:
            return {"error": str(exc)}


def _make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json", headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                page = WEB_HTML.replace("__PB_VERSION__", VERSION)
                self._send(200, page.encode(), "text/html; charset=utf-8",
                           {"Cache-Control": "no-store", "Pragma": "no-cache"})
            elif u.path == "/api/state":
                try:
                    since = int(parse_qs(u.query).get("since", ["0"])[0] or 0)
                except Exception:
                    since = 0
                self._send(200, state.public_state(since))
            elif u.path == "/api/browse":
                path = parse_qs(u.query).get("path", [""])[0]
                self._send(200, browse_dir(path))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, {"error": "bad request"})
            route = self.path
            appid = str(body.get("appid", ""))
            cfg = state.cfg

            if route == "/api/launch":
                return self._send(200, state.launch(appid, body.get("mode", "auto"),
                                                    bool(body.get("confirm"))))
            if route == "/api/cancel":
                state.cancel.set()
                return self._send(200, {"ok": True})
            if route == "/api/answer":
                state.answer(body.get("ok"))
                return self._send(200, {"ok": True})
            if route == "/api/end-session":
                return self._send(200, state.end_session(appid))
            if route == "/api/backup":
                return self._send(200, state.backup(appid, body.get("save_path")))
            if route == "/api/restore":
                return self._send(200, state.restore(appid, body.get("save_path")))
            if route == "/api/rescan":
                state.refresh()
                return self._send(200, {"ok": True})
            if route == "/api/select":
                cfg["last_game"] = appid
                save_config(cfg)
                return self._send(200, {"ok": True})
            if route == "/api/proton":
                overrides = cfg.setdefault("proton_override", {})
                if body.get("path"):
                    overrides[appid] = body["path"]
                else:
                    overrides.pop(appid, None)
                save_config(cfg)
                state.bump()
                return self._send(200, {"ok": True})
            if route == "/api/settings":
                settings = cfg_game_settings(cfg, appid)
                settings["close_apps_on_exit"] = bool(body.get("close_apps_on_exit"))
                save_config(cfg)
                state.bump()
                return self._send(200, {"ok": True})
            if route == "/api/apps/add":
                path = str(body.get("path", "")).strip()
                if not path:
                    return self._send(200, {"error": "No path given."})
                cfg_apps(cfg, appid).append({"path": path, "args": ""})
                save_config(cfg)
                state.bump()
                state.log(f"Added companion app for App ID {appid}: {path}")
                return self._send(200, {"ok": True})
            if route == "/api/apps/remove":
                apps = cfg_apps(cfg, appid)
                idx = int(body.get("index", -1))
                if 0 <= idx < len(apps):
                    removed = apps.pop(idx)
                    save_config(cfg)
                    state.bump()
                    state.log(f"Removed companion app: {removed['path']}")
                return self._send(200, {"ok": True})
            if route == "/api/apps/args":
                apps = cfg_apps(cfg, appid)
                idx = int(body.get("index", -1))
                if 0 <= idx < len(apps):
                    apps[idx]["args"] = str(body.get("args", "")).strip()
                    save_config(cfg)
                return self._send(200, {"ok": True})
            if route == "/api/shortcut":
                game = state.game(appid)
                if not game:
                    return self._send(200, {"error": "Unknown game."})
                try:
                    create_game_shortcut(game, state.log)
                    return self._send(200, {"ok": True})
                except Exception as exc:
                    return self._send(200, {"error": str(exc)})
            if route == "/api/install-self":
                try:
                    create_self_shortcut(state.log)
                    return self._send(200, {"ok": True})
                except Exception as exc:
                    return self._send(200, {"error": str(exc)})
            return self._send(404, {"error": "not found"})

    return Handler


CHROMIUM_NAMES = ("chromium", "chromium-browser", "google-chrome-stable",
                  "google-chrome", "brave-browser", "brave", "microsoft-edge",
                  "microsoft-edge-stable", "vivaldi", "vivaldi-stable", "opera")
FLATPAK_BROWSERS = ("org.chromium.Chromium", "com.google.Chrome",
                    "com.brave.Browser", "com.microsoft.Edge",
                    "com.opera.Opera", "com.vivaldi.Vivaldi")


def detect_browsers():
    """Chromium-family browsers usable for app-window mode, native
    installs first, then Flatpaks. Returns [(kind, label, cmd_prefix)]."""
    found = []
    for name in CHROMIUM_NAMES:
        exe = shutil.which(name)
        if exe:
            found.append(("native", name, [exe]))
    for base in (Path.home() / ".local/share/flatpak/exports/bin",
                 Path("/var/lib/flatpak/exports/bin")):
        if base.is_dir():
            for appid in FLATPAK_BROWSERS:
                if (base / appid).exists():
                    found.append(("flatpak", appid, ["flatpak", "run", appid]))
    return found


def _try_pywebview_shell(url):
    """Native window via pywebview (if the user installed it).
    Blocks until the window closes. Returns True if it ran."""
    try:
        import webview
    except Exception:
        return False
    try:
        webview.create_window(APP_NAME, url, width=1280, height=920,
                              min_size=(960, 700))
        webview.start()
        return True
    except Exception as exc:
        print(f"pywebview shell failed: {exc!r}")
        return False


def _try_gtk_shell(url, quiet=False):
    """Native window via WebKitGTK directly (python3-gobject +
    webkitgtk6.0, or the older webkit2gtk4.x). No browser involved —
    a real application window. Blocks until it closes."""
    # WebKitGTK workarounds that matter on real hardware: its sandbox
    # cannot start on some systems, and its DMABUF renderer breaks on
    # NVIDIA/Wayland (Hussein's exact setup). The page is our own
    # local UI, so both escape hatches are safe to set here.
    os.environ.setdefault("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", "1")
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    try:
        import gi
    except Exception as exc:
        if not quiet:
            print(f"GTK shell unavailable: cannot import gi ({exc!r})")
        return False
    # WebKit 6.0 (GTK4) first.
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("WebKit", "6.0")
        from gi.repository import Gtk, WebKit

        app = Gtk.Application(application_id="com.protonbuddy.app")

        def on_activate(app):
            win = Gtk.ApplicationWindow(application=app)
            win.set_title(APP_NAME)
            win.set_default_size(1280, 920)
            wv = WebKit.WebView()
            wv.load_uri(url)
            win.set_child(wv)
            win.present()

        app.connect("activate", on_activate)
        app.run(sys.argv)
        return True
    except Exception as exc:
        if not quiet:
            print(f"GTK4/WebKit6 shell failed: {exc!r}")
    # WebKit2 4.x (GTK3).
    last_exc = None
    for ver in ("4.1", "4.0"):
        try:
            gi.require_version("Gtk", "3.0")
            gi.require_version("WebKit2", ver)
            from gi.repository import Gtk, WebKit2

            win = Gtk.Window(title=APP_NAME)
            win.set_default_size(1280, 920)
            wv = WebKit2.WebView()
            wv.load_uri(url)
            win.add(wv)
            win.connect("destroy", Gtk.main_quit)
            win.show_all()
            Gtk.main()
            return True
        except Exception as exc:
            last_exc = exc
            continue
    if not quiet and last_exc is not None:
        print(f"GTK3/WebKit2 shell failed: {last_exc!r}")
    return False


def _try_qt_shell(url):
    """Native window via Qt WebEngine (PySide6 or PyQt6), if present.
    KDE systems often have the Qt libraries already. Blocks until
    the window closes."""
    app_cls = view_cls = None
    try:
        from PySide6.QtWidgets import QApplication, QMainWindow
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtCore import QUrl
        app_cls, view_cls = (QApplication, QMainWindow, QWebEngineView, QUrl)
    except Exception:
        try:
            from PyQt6.QtWidgets import QApplication, QMainWindow
            from PyQt6.QtWebEngineWidgets import QWebEngineView
            from PyQt6.QtCore import QUrl
            app_cls = (QApplication, QMainWindow, QWebEngineView, QUrl)
        except Exception:
            return False
    try:
        QApplication_, QMainWindow_, QWebEngineView_, QUrl_ = app_cls
        app = QApplication_(sys.argv)
        app.setApplicationName(APP_NAME)
        win = QMainWindow_()
        win.setWindowTitle(APP_NAME)
        win.resize(1280, 920)
        view = QWebEngineView_()
        view.load(QUrl_(url))
        win.setCentralWidget(view)
        win.show()
        app.exec()
        return True
    except Exception as exc:
        print(f"Qt shell failed: {exc!r}")
        return False


def _open_chromium_app(url):
    """Chromium-family --app window (native or Flatpak). Returns the
    Popen handle (so the program can exit when the window closes),
    or None if no such browser exists / launching failed."""
    profile = CONFIG_DIR / "browser-profile"
    for kind, label, prefix in detect_browsers():
        try:
            args = list(prefix)
            if kind == "native":
                args.append(f"--user-data-dir={profile}")
            args += [f"--app={url}", "--no-first-run", "--window-size=1280,920"]
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            print(f"Opened as a Chromium app window (via {label}, {kind}).")
            return proc
        except Exception:
            continue
    return None

def _existing_instance():
    """Port of an already-running Proton Buddy server, if any. Two
    copies alive at once means windows from different versions pile
    up on different ports, and the user ends up looking at an old
    window while the new one hides behind it."""
    import http.client
    for port in range(8765, 8785):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.4)
            conn.request("GET", "/api/state")
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            if resp.status == 200 and b'"protons"' in data and b'"games"' in data:
                return port
        except Exception:
            continue
    return None


def run_gui():
    existing = _existing_instance()
    if existing:
        print("Another Proton Buddy is already running "
              f"(its service answers on port {existing}) — probably an older "
              "version whose window is still open. Close that window and its "
              "terminal first, or run:  pkill -f proton_buddy.py")
        return
    state = BuddyWebState()
    state.log(f"Proton Buddy started — {len(state.games)} games and "
              f"{len(state.catalog)} Proton runtimes across "
              f"{len(state.lib_owner)} libraries.")
    server = None
    for port in range(8765, 8785):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state))
            break
        except OSError:
            continue
    if server is None:
        print("Proton Buddy could not start its local service (no free port).")
        print("The command line still works: proton_buddy.py --list / --launch <APPID>")
        return
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    # The engine serves in the background; the window owns the program.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Proton Buddy v{VERSION} — service at {url}")

    if os.environ.get("PB_NO_BROWSER"):
        print("PB_NO_BROWSER set — serving headless. Ctrl+C to stop.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return

    # 1) A real native window: pywebview, WebKitGTK, then Qt WebEngine.
    if _try_pywebview_shell(url):
        print("Proton Buddy closed.")
        return
    if _try_gtk_shell(url):
        print("Proton Buddy closed.")
        return
    if _try_qt_shell(url):
        print("Proton Buddy closed.")
        return

    # 2) Chromium app window; the program lives as long as it does.
    proc = _open_chromium_app(url)
    if proc is not None:
        print("Close the Proton Buddy window to quit.")
        try:
            proc.wait()
        except KeyboardInterrupt:
            try:
                proc.terminate()
            except Exception:
                pass
        print("Proton Buddy closed.")
        return

    # 3) Last resort: a plain browser tab.
    print("No native-window library and no Chromium-family browser found, "
          "so the UI opens as a browser tab. For a real app window, install "
          "the WebKitGTK libraries once:\n"
          "    sudo dnf install -y python3-gobject webkitgtk6.0\n"
          "Keep this terminal open while you use Proton Buddy (Ctrl+C to stop).")
    try:
        webbrowser.open(url)
    except Exception:
        print(f"Open {url} in your browser.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Proton Buddy v1.0.3 — auto companion-app launcher for Steam/Proton")
    ap.add_argument("--list", action="store_true",
                    help="List detected games and Proton runtimes, then exit")
    ap.add_argument("--browser-check", action="store_true",
                    help="Show which browsers were detected for app-window mode, then exit")
    ap.add_argument("--window-check", action="store_true",
                    help="Diagnose the native-window libraries step by step, then exit")
    ap.add_argument("--launch", metavar="APPID",
                    help="Auto-launch a game and its saved companion apps")
    ap.add_argument("--app", action="append", default=[], metavar="EXE",
                    help="Extra companion .exe to launch (with --launch); repeatable")
    args = ap.parse_args(argv)

    roots = steam_roots()
    lib_owner = all_libraries(roots)
    mapping = {}
    for r in roots:
        mapping.update(parse_compat_tool_mapping(r))

    if args.browser_check:
        found = detect_browsers()
        if found:
            print("Chromium-family browsers detected (app-window mode available):")
            for kind, label, prefix in found:
                print(f"  {label} ({kind})  ->  {' '.join(prefix)}")
        else:
            print("No Chromium-family browser detected (checked Chrome, Chromium, "
                  "Brave, Edge, Vivaldi, Opera — native installs and Flatpaks).")
            print("Proton Buddy's UI will open as a tab in the default browser.")
            print(f"Default browser handler: {webbrowser.get().name}")
        return 0

    if args.window_check:
        print("Proton Buddy — native window diagnosis")
        print("=====================================")

        def step(name, fn):
            try:
                fn()
                print(f"  OK    {name}")
                return True
            except Exception as exc:
                print(f"  FAIL  {name}: {exc!r}")
                return False

        step("import pywebview", lambda: __import__("webview"))
        gi_ok = step("import gi (python3-gobject)", lambda: __import__("gi"))
        if gi_ok:
            import gi

            def gtk4_real_attempt():
                os.environ.setdefault("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", "1")
                os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
                gi.require_version("Gtk", "4.0")
                gi.require_version("WebKit", "6.0")
                from gi.repository import GLib, Gtk, WebKit
                app = Gtk.Application(application_id="com.protonbuddy.check")

                def on_activate(a):
                    win = Gtk.ApplicationWindow(application=a)
                    win.set_title("Proton Buddy — window check")
                    win.set_default_size(480, 320)
                    wv = WebKit.WebView()
                    wv.load_uri("about:blank")
                    win.set_child(wv)
                    win.present()
                    GLib.timeout_add(1500, lambda: (a.quit(), False)[1])

                app.connect("activate", on_activate)
                app.run(sys.argv)

            if step("GTK4 + WebKit 6.0 real window (flashes for ~2s)", gtk4_real_attempt):
                print("  -> The native GTK window works. Proton Buddy should "
                      "open as its own program.")
            else:
                for ver in ("4.1", "4.0"):
                    step(f"require WebKit2 {ver}",
                         lambda v=ver: gi.require_version("WebKit2", v))
        step("import PySide6 WebEngine", lambda: __import__("PySide6.QtWebEngineWidgets",
                                                            fromlist=["QWebEngineView"]))
        step("import PyQt6 WebEngine", lambda: __import__("PyQt6.QtWebEngineWidgets",
                                                          fromlist=["QWebEngineView"]))
        found = detect_browsers()
        print(f"  Chromium-family browsers for --app mode: "
              f"{[(k, l) for k, l, _ in found] or 'none'}")
        print("Paste this whole output to Muse — it shows exactly which "
              "step fails on this machine.")
        return 0

    if args.list or args.launch:
        games = discover_games(roots, lib_owner)
        catalog = discover_protons(roots, list(lib_owner.keys()))
        if args.list:
            print(f"Steam roots: {[str(r) for r in roots]}")
            print(f"Libraries ({len(lib_owner)}):")
            for lib in lib_owner:
                print(f"  {lib}")
            print(f"\nProton runtimes ({len(catalog)}):")
            for t in catalog:
                print(f"  {t['name']}  [{t['kind']}]  {t['path']}")
            print(f"\nGames ({len(games)}):")
            for g in games:
                tool, why = auto_proton_for(g["appid"], catalog, mapping)
                print(f"  {g['name']} (App ID {g['appid']})")
                print(f"    library: {g['library']}")
                print(f"    prefix:  {g['prefix'] or 'not created yet'}")
                print(f"    proton:  {tool['name'] if tool else 'NONE'} ({why})")
            return 0
        game = next((g for g in games if g["appid"] == args.launch), None)
        if not game:
            print(f"No installed game with App ID {args.launch}.")
            return 1
        cfg = read_config()
        apps = [dict(a) for a in cfg_apps(cfg, game["appid"])]
        for extra in args.app:
            apps.append({"path": extra, "args": ""})
        apps = [a for a in apps if Path(a["path"]).is_file()]
        if not apps:
            print("No companion apps configured for this game. "
                  "Add them in the GUI, or pass --app /path/to/tool.exe")
            return 1
        override = cfg.get("proton_override", {}).get(game["appid"])
        tool = next((t for t in catalog if t["path"] == override), None)
        if not tool:
            tool, _ = auto_proton_for(game["appid"], catalog, mapping)
        if not tool:
            print("No Proton runtime found on any disk.")
            return 1
        ok, popens = auto_launch(game, tool, apps, cli_log, threading.Event(), catalog=catalog)
        if ok:
            notify(APP_NAME, f"{game['name']} launched with {len(popens)} companion app(s).")
            settings = cfg_game_settings(cfg, game["appid"])
            if popens and settings.get("close_apps_on_exit"):
                # Headless babysitter: this process stays alive
                # (invisibly) until the game exits, closes the apps,
                # then ends — the desktop shortcut flow.
                watch_game_session(game, popens, cli_log, threading.Event())
        else:
            notify(APP_NAME, f"{game['name']} launch did not complete — see the Proton Buddy log.")
        return 0 if ok else 1

    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
