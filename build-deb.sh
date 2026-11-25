#!/bin/bash
# Build the Clawputer .deb (run on the CardputerZero or any Debian box with dpkg-deb).
#   ./build-deb.sh            -> dist/clawputer_<version>_all.deb
set -euo pipefail
cd "$(dirname "$0")"
VERSION=$(cat VERSION)
PKG=build/pkg
rm -rf build; mkdir -p "$PKG/DEBIAN" dist; chmod 755 "$PKG"

A="$PKG/usr/share/APPLaunch"
S="$PKG/usr/share/clawputer"
install -d "$A/applications" "$A/bin" "$A/share/images" "$S/boost" "$PKG/usr/share/doc/clawputer"

install -m 755 src/clawputer "$A/bin/clawputer"
install -m 644 assets/clawputer.png "$A/share/images/clawputer.png"
cat > "$A/applications/clawputer.desktop" <<'EOF'
[Desktop Entry]
Name=Clawputer
Exec=/usr/share/APPLaunch/bin/clawputer
Terminal=true
Icon=share/images/clawputer.png
Type=Application
EOF
chmod 644 "$A/applications/clawputer.desktop"

install -m 644 src/clawputer_gui.py src/clawputer_setup.py src/CLAUDE.md src/CLAUDE.remote.md VERSION "$S/"
install -m 755 src/boost/clawputer-boost src/boost/clawputer-bootmode src/boost/enable-boost.sh src/boost/disable-boost.sh "$S/boost/"
install -m 644 LICENSE "$PKG/usr/share/doc/clawputer/copyright"

cat > "$PKG/DEBIAN/control" <<EOF
Package: clawputer
Version: $VERSION
Architecture: all
Maintainer: RubberDingyRapids <RubberDingyRapids@users.noreply.github.com>
Section: APPLaunch
Priority: optional
Homepage: https://github.com/RubberDingyRapids/clawputer
Depends: python3, python3-pil, python3-numpy, python3-qrcode, curl, openssh-client, fonts-dejavu-core
Description: Pocket chat app for Claude Code on CardputerZero (unofficial)
 A compact chat GUI for Anthropic's Claude Code made for the CardputerZero's
 320x170 screen and keyboard: code boxes, chat history, settings, permission
 prompts, questions, drafts, QR-code sign-in and an optional remote mode that
 runs Claude on your PC. Not affiliated with Anthropic.
EOF

# on uninstall, undo the optional RAM boost if the user had enabled it
cat > "$PKG/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
  [ -x /usr/share/clawputer/boost/disable-boost.sh ] && [ -e /etc/sudoers.d/clawputer ] && \
    /usr/share/clawputer/boost/disable-boost.sh || true
fi
exit 0
EOF
chmod 755 "$PKG/DEBIAN/prerm"

# python byte-code would be root-owned clutter; keep the package source-only
find "$PKG" -name '__pycache__' -prune -exec rm -rf {} +
OUT="dist/clawputer_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$PKG" "$OUT" >/dev/null
echo "built $OUT ($(du -h "$OUT" | cut -f1))"
