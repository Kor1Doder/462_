#!/usr/bin/env bash
# Launch the cncctl operator GUI fullscreen inside the kiosk labwc session
# (Raspberry Pi OS Trixie). The kiosk labwc's autostart runs this script (see
# deploy/cncctl-kiosk.service + install.sh), so WAYLAND_DISPLAY is already set.
#
# The GUI is relaunched if it exits or crashes — an operator panel should always
# be up. Escape hatch: `touch ~/.cncctl-stop` stops the loop; switch to another
# VT (Ctrl-Alt-F2) or SSH in for maintenance, then `rm ~/.cncctl-stop`.
set -uo pipefail   # not -e: the relaunch loop must survive a non-zero exit

# Self-locate the repo root (this script lives in <repo>/deploy/).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# We run under labwc (Wayland); fall back to xcb/XWayland if the Qt Wayland
# plugin is unavailable. Pi 5 (V3D/Mesa) provides the EGL/GLES the 3D view uses.
export QT_QPA_PLATFORM="wayland;xcb"
export PYTHONUNBUFFERED=1

while true; do
    "${REPO_ROOT}/.venv/bin/python" examples/gui.py \
        --config config/machine.toml --fullscreen || true
    # A user-demanded "Quit -> Desktop" drops this sentinel: stay closed for the
    # session (the service clears it on its next start / on reboot). Any other
    # exit — a crash, or a frozen GUI that got killed — leaves no sentinel, so
    # the panel relaunches.
    if [ -e "${HOME}/.cncctl-stop" ]; then
        break
    fi
    sleep 2
done
