#!/usr/bin/env bash
# Undo deploy/install.sh: stop/remove the kiosk service, its isolated config,
# and the udev rule. Leaves the repo, the .venv, and group memberships in place.
#
#   sudo deploy/uninstall.sh [--user <name>] [--restore-desktop]
set -euo pipefail

CNC_USER="${SUDO_USER:-${USER}}"
RESTORE_DESKTOP=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) CNC_USER="$2"; shift 2 ;;
        --restore-desktop) RESTORE_DESKTOP=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo deploy/uninstall.sh" >&2
    exit 1
fi
CNC_HOME="$(getent passwd "${CNC_USER}" | cut -d: -f6)"

echo ">> Stopping and removing the kiosk service ..."
systemctl disable --now cncctl-kiosk.service 2>/dev/null || true
rm -f /etc/systemd/system/cncctl-kiosk.service
systemctl daemon-reload

echo ">> Removing the isolated kiosk config ..."
rm -rf "${CNC_HOME}/.cncctl-kiosk"

echo ">> Removing any leftover desktop-autostart entry ..."
DESKTOP_AUTOSTART="${CNC_HOME}/.config/labwc/autostart"
if [[ -f "${DESKTOP_AUTOSTART}" ]]; then
    sudo -u "${CNC_USER}" -H sed -i '/# >>> cncctl >>>/,/# <<< cncctl <<</d' "${DESKTOP_AUTOSTART}"
fi

echo ">> Removing the quit-to-desktop helper and sudoers rule ..."
rm -f /usr/local/sbin/cncctl-to-desktop
rm -f /etc/sudoers.d/cncctl-kiosk

echo ">> Removing the udev rule ..."
rm -f /etc/udev/rules.d/99-cncctl-grblhal.rules
udevadm control --reload-rules || true

if [[ "${RESTORE_DESKTOP}" -eq 1 ]]; then
    echo ">> Restoring desktop boot ..."
    if command -v raspi-config >/dev/null 2>&1; then
        raspi-config nonint do_boot_behaviour B4 || true   # desktop + autologin
    else
        systemctl set-default graphical.target || true
    fi
fi

echo ">> Done."
