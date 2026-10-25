#!/bin/sh
# Build Proton_Buddy-<version>-x86_64.AppImage (portable Python + the app).
# Requires: curl, tar, strip, and appimagetool on PATH (AppImageKit).
set -e
cd "$(dirname "$0")"
VERSION=$(grep -m1 'VERSION = ' proton_buddy.py | cut -d'"' -f2)
WORK=$(mktemp -d)
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.12.7+20241016-x86_64-unknown-linux-gnu-install_only.tar.gz"
curl -sL -o "$WORK/python.tar.gz" "$PY_URL"
tar xzf "$WORK/python.tar.gz" -C "$WORK"
APPDIR="$WORK/ProtonBuddy.AppDir"
mkdir -p "$APPDIR/usr/share/proton-buddy" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -r "$WORK/python/"* "$APPDIR/usr/"
cp proton_buddy.py README.md "$APPDIR/usr/share/proton-buddy/"
cp icon.png "$APPDIR/proton-buddy.png"
cp icon.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/proton-buddy.png"
cat > "$APPDIR/proton-buddy.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=Proton Buddy
Comment=Launch Steam games with companion apps in the same Proton prefix
Exec=proton-buddy
Icon=proton-buddy
Categories=Game;Utility;
Terminal=false
DESK
cp "$APPDIR/proton-buddy.desktop" "$APPDIR/usr/share/applications/"
cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
APP="$HERE/usr/share/proton-buddy/proton_buddy.py"
unset LD_LIBRARY_PATH PYTHONHOME PYTHONPATH
if command -v python3 >/dev/null 2>&1; then
  if python3 -c "import gi; gi.require_version('WebKit', '6.0'); from gi.repository import WebKit" >/dev/null 2>&1 \
     || python3 -c "import gi; gi.require_version('WebKit2', '4.1'); from gi.repository import WebKit2" >/dev/null 2>&1; then
    exec python3 "$APP" "$@"
  fi
fi
exec "$HERE/usr/bin/python3" "$APP" "$@"
RUN
chmod +x "$APPDIR/AppRun"
# trim the bundle: headers, tests, tkinter, debug symbols
rm -rf "$APPDIR/usr/include" "$APPDIR/usr/lib/python3.12/test" \
       "$APPDIR/usr/lib/python3.12/idlelib" "$APPDIR/usr/lib/python3.12/tkinter" \
       "$APPDIR/usr/lib/python3.12/turtledemo" "$APPDIR/usr/lib/python3.12/ensurepip" \
       "$APPDIR/usr/lib/tcl8.6" "$APPDIR/usr/lib/tk8.6" "$APPDIR/usr/lib/Tix8.4.3" \
       "$APPDIR/usr/lib/python3.12/lib-dynload/_tkinter"*.so
find "$APPDIR/usr" -name "*.so*" -exec strip {} + 2>/dev/null || true
ARCH=x86_64 appimagetool "$APPDIR" "Proton_Buddy-${VERSION}-x86_64.AppImage"
echo "Built Proton_Buddy-${VERSION}-x86_64.AppImage"
