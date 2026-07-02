#!/usr/bin/env bash
# One-time setup so the CNC panel is one-click AND opens automatically on boot —
# without the full kiosk takeover. Run once on the Pi:
#
#   bash ~/cncctl/deploy/install-launcher.sh
#
# After this: a "CNC Panel" icon sits on the Desktop (double-click to launch),
# and it also starts by itself whenever the Pi boots to the desktop.
set -euo pipefail

PROJ="$(cd "$(dirname "$0")/.." && pwd)"   # project root (the folder with run-gui.sh)
chmod +x "$PROJ/run-gui.sh"

ENTRY="[Desktop Entry]
Type=Application
Name=CNC Panel
Comment=dokan CNC controller
Exec=$PROJ/run-gui.sh --fullscreen
Path=$PROJ
Icon=applications-engineering
Terminal=false
Categories=Utility;
X-GNOME-Autostart-enabled=true"

# 1) One-click icon on the Desktop.
DESKTOP="${XDG_DESKTOP_DIR:-$HOME/Desktop}"
if [ -d "$DESKTOP" ]; then
  printf '%s\n' "$ENTRY" > "$DESKTOP/cncctl.desktop"
  chmod +x "$DESKTOP/cncctl.desktop"
  gio set "$DESKTOP/cncctl.desktop" metadata::trusted true 2>/dev/null || true
  echo "Desktop icon:   $DESKTOP/cncctl.desktop"
fi

# 2) Auto-open on every desktop login/boot.
AUTOSTART="$HOME/.config/autostart"
mkdir -p "$AUTOSTART"
printf '%s\n' "$ENTRY" > "$AUTOSTART/cncctl.desktop"
echo "Auto-start:     $AUTOSTART/cncctl.desktop"

echo
echo "Done. Double-click 'CNC Panel' on the Desktop, or just reboot — it opens by itself."
echo "(To stop auto-start later: rm $AUTOSTART/cncctl.desktop)"
