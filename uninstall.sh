#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_FILE="${HOME}/.local/share/applications/wayclick-autoclicker.desktop"
ICON_SVG_DEST="${HOME}/.local/share/icons/hicolor/scalable/apps/wayclick-autoclicker.svg"
ICON_PNG_DEST="${HOME}/.local/share/icons/hicolor/256x256/apps/wayclick-autoclicker.png"

echo "=== WayClick Clean Uninstall ==="

# 1. Remove desktop shortcut and freedesktop icon files if present
for target in "${DESKTOP_FILE}" "${ICON_SVG_DEST}" "${ICON_PNG_DEST}"; do
    if [[ -f "${target}" ]]; then
        rm -f "${target}"
        echo "[Removed] ${target}"
    fi
done
update-desktop-database "${HOME}/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" 2>/dev/null || true

# 2. Remove local bytecode cache & config inside workspace folder
rm -rf "${SCRIPT_DIR}/__pycache__"
if [[ "${1:-}" == "--purge-all" ]]; then
    echo "[Purging] Removing entire project directory: ${SCRIPT_DIR}"
    rm -rf "${SCRIPT_DIR}"
    echo "[Done] 100% removed with zero system residuals."
else
    rm -f "${SCRIPT_DIR}/config.json"
    echo "[Cleaned] Removed local config.json and __pycache__."
    echo "Tip: Run './uninstall.sh --purge-all' to also delete the ${SCRIPT_DIR} folder itself."
fi
