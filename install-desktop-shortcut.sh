#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_FILE="${HOME}/.local/share/applications/wayclick-autoclicker.desktop"
ICON_SVG_DEST="${HOME}/.local/share/icons/hicolor/scalable/apps/wayclick-autoclicker.svg"
ICON_PNG_DEST="${HOME}/.local/share/icons/hicolor/256x256/apps/wayclick-autoclicker.png"

mkdir -p "$(dirname "${DESKTOP_FILE}")"
mkdir -p "$(dirname "${ICON_SVG_DEST}")"
mkdir -p "$(dirname "${ICON_PNG_DEST}")"

cp -f "${SCRIPT_DIR}/icon.svg" "${ICON_SVG_DEST}"
cp -f "${SCRIPT_DIR}/icon.png" "${ICON_PNG_DEST}"

cat > "${DESKTOP_FILE}" <<EOF
[Desktop Entry]
Type=Application
Name=WayClick Auto Clicker
GenericName=Auto Clicker & Key Presser
Comment=Wayland-native kernel-level auto clicker and key presser (evdev/uinput)
Exec=${SCRIPT_DIR}/run.sh
Icon=${SCRIPT_DIR}/icon.svg
StartupWMClass=autoclicker.py
Terminal=false
Categories=Utility;Game;
Keywords=autoclicker;clicker;macro;mouse;keyboard;wayland;
StartupNotify=true
EOF

chmod +x "${DESKTOP_FILE}"
update-desktop-database "${HOME}/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" 2>/dev/null || true

echo "[OK] Installed KDE/Desktop launcher and custom icons:"
echo "     • Launcher: ${DESKTOP_FILE}"
echo "     • SVG Icon: ${ICON_SVG_DEST}"
echo "     • PNG Icon: ${ICON_PNG_DEST}"
