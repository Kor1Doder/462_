#!/usr/bin/env bash
# One-shot installer: turn a Raspberry Pi 5 running Raspberry Pi OS Trixie
# (64-bit) into a cncctl CNC operator-panel appliance that boots straight into
# the GUI, fullscreen, with no desktop or taskbar.
#
#   git clone <this repo> ~/cncctl
#   cd ~/cncctl
#   sudo deploy/install.sh
#
# Trixie's only packaged Wayland compositor is labwc (cage/weston/wayfire were
# dropped), so we run our OWN minimal labwc instance as a systemd service on
# VT1 with an isolated config — it starts only the GUI, never the desktop panel.
# Re-runnable (idempotent). What it does:
#   1. apt-installs labwc + the Qt/OpenGL/Wayland runtime libraries.
#   2. Creates a Python venv at <repo>/.venv and pip-installs requirements.
#   3. Adds the operator user to the dialout/render/video/input groups.
#   4. Installs the udev rule for the grblHAL Pico.
#   5. Installs the labwc kiosk systemd service, the "Quit -> Desktop" helper
#      (+ scoped sudoers rule), and sets console boot (desktop on demand).
#
# Flags:
#   --no-boot-change  Do not change the boot behaviour (leave the desktop on —
#                     you must then disable it yourself for the kiosk to start).
#   --user <name>     Operator account to run the GUI as (default: the sudo user).
set -euo pipefail

# --- resolve paths and the operator user -----------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BOOT_CHANGE=1
CNC_USER="${SUDO_USER:-${USER}}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-boot-change) BOOT_CHANGE=0; shift ;;
        --user) CNC_USER="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "This installer must run as root: sudo deploy/install.sh" >&2
    exit 1
fi
if [[ "${CNC_USER}" == "root" ]]; then
    echo "Refusing to run the panel as root. Re-run with: sudo deploy/install.sh --user <name>" >&2
    exit 1
fi
CNC_HOME="$(getent passwd "${CNC_USER}" | cut -d: -f6)"
echo ">> repo:   ${REPO_ROOT}"
echo ">> user:   ${CNC_USER} (${CNC_HOME})"

run_as_user() { sudo -u "${CNC_USER}" -H "$@"; }

# --- 1. system packages -----------------------------------------------------
echo ">> [1/5] Installing system packages (apt) ..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# labwc is already present on the Desktop image; listed so a re-image / Lite host
# still gets it. The libs back PySide6's Wayland + OpenGL (3D workpiece view).
apt-get install -y --no-install-recommends \
    labwc \
    libegl1 libgl1 libgles2 libglu1-mesa libgl1-mesa-dri \
    libxkbcommon0 libwayland-client0 libwayland-cursor0 libwayland-egl1 \
    libdbus-1-3 fontconfig libfreetype6 fonts-dejavu-core \
    libxcb-cursor0 libxcb-xinerama0 \
    python3 python3-venv python3-dev \
    git curl

# --- 2. Python venv (>=3.12) + dependencies --------------------------------
echo ">> [2/5] Creating the Python virtual environment ..."
VENV="${REPO_ROOT}/.venv"
pick_python() {
    if command -v python3.12 >/dev/null 2>&1; then echo "python3.12"; return; fi
    # Trixie ships Python 3.13, which satisfies our >=3.12 requirement.
    if python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,12) else 1)' 2>/dev/null; then
        echo "python3"; return
    fi
    echo ""  # none suitable
}
PYBIN="$(pick_python)"
if [[ -n "${PYBIN}" ]]; then
    run_as_user "${PYBIN}" -m venv "${VENV}"
else
    # Older image with Python < 3.12: provision one with uv (standalone build).
    echo "   No system Python >= 3.12 found; provisioning one with uv ..."
    if ! run_as_user bash -lc 'command -v uv >/dev/null 2>&1'; then
        run_as_user bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    fi
    # --seed installs pip into the venv so the uniform `python -m pip` path works.
    run_as_user bash -lc "\$HOME/.local/bin/uv venv --seed --python 3.12 '${VENV}'"
fi
echo ">> Installing requirements.txt into the venv (aarch64 wheels) ..."
run_as_user "${VENV}/bin/python" -m pip install --upgrade pip
run_as_user "${VENV}/bin/python" -m pip install -r "${REPO_ROOT}/requirements.txt"
# Install the cncctl package itself (editable, deps already satisfied above) so
# `import cncctl` resolves and the `cncctl` CLI is available. Editable means a
# later `git pull` is picked up without reinstalling.
echo ">> Installing the cncctl package (editable) ..."
run_as_user "${VENV}/bin/python" -m pip install -e "${REPO_ROOT}" --no-deps

# --- 3. groups: serial + GPU + input ---------------------------------------
echo ">> [3/5] Adding ${CNC_USER} to dialout/render/video/input groups ..."
usermod -aG dialout,render,video,input "${CNC_USER}"

# --- 4. udev rule for the grblHAL Pico -------------------------------------
echo ">> [4/5] Installing the grblHAL udev rule ..."
install -m 0644 "${SCRIPT_DIR}/99-cncctl-grblhal.rules" /etc/udev/rules.d/99-cncctl-grblhal.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty || true

# --- 5. labwc kiosk service + console boot ----------------------------------
echo ">> [5/5] Installing the labwc kiosk service ..."
chmod +x "${SCRIPT_DIR}/start-kiosk.sh"

# Clean up the previous desktop-autostart approach, if it was ever installed.
DESKTOP_AUTOSTART="${CNC_HOME}/.config/labwc/autostart"
if [[ -f "${DESKTOP_AUTOSTART}" ]]; then
    run_as_user sed -i '/# >>> cncctl >>>/,/# <<< cncctl <<</d' "${DESKTOP_AUTOSTART}"
fi

# Isolated config home: the kiosk labwc starts ONLY our GUI — no desktop panel
# (the panel peeking through dialogs was the desktop session's wf-panel-pi).
XDG_KIOSK="${CNC_HOME}/.cncctl-kiosk"
run_as_user mkdir -p "${XDG_KIOSK}/labwc"
run_as_user tee "${XDG_KIOSK}/labwc/autostart" >/dev/null <<EOF
#!/bin/sh
# Run by the kiosk labwc instance (deploy/cncctl-kiosk.service).
"${REPO_ROOT}/deploy/start-kiosk.sh" &
EOF
# rc.xml: keep it minimal but force every window fullscreen/undecorated so even
# the GUI's dialogs cover the screen edge-to-edge.
run_as_user tee "${XDG_KIOSK}/labwc/rc.xml" >/dev/null <<'EOF'
<?xml version="1.0"?>
<labwc_config>
  <core><gap>0</gap></core>
  <windowRules>
    <windowRule identifier="*" serverDecoration="no"/>
  </windowRules>
</labwc_config>
EOF

UNIT=/etc/systemd/system/cncctl-kiosk.service
sed -e "s|__CNC_USER__|${CNC_USER}|g" \
    -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__XDG_CONFIG_HOME__|${XDG_KIOSK}|g" \
    -e "s|__CNC_HOME__|${CNC_HOME}|g" \
    "${SCRIPT_DIR}/cncctl-kiosk.service" > "${UNIT}"
systemctl daemon-reload
systemctl enable cncctl-kiosk.service

# "Quit -> Desktop": a root-owned helper that switches to the desktop, plus a
# scoped NOPASSWD sudoers rule so the (unprivileged) GUI can invoke just that.
echo ">> Installing the quit-to-desktop helper ..."
install -m 0755 -o root -g root "${SCRIPT_DIR}/cncctl-to-desktop" /usr/local/sbin/cncctl-to-desktop
SUDOERS=/etc/sudoers.d/cncctl-kiosk
echo "${CNC_USER} ALL=(root) NOPASSWD: /usr/local/sbin/cncctl-to-desktop" > "${SUDOERS}"
chmod 0440 "${SUDOERS}"
if ! visudo -cf "${SUDOERS}" >/dev/null; then
    echo "   sudoers check failed; removing ${SUDOERS}" >&2
    rm -f "${SUDOERS}"
fi

# Boot to console so the kiosk owns the display at boot (no desktop competing
# for DRM), but keep the desktop reachable: B4 configures the desktop + its
# autologin, then we point the default target back at multi-user so boot lands
# in the kiosk. "Quit -> Desktop" later isolates graphical.target on demand.
if [[ "${BOOT_CHANGE}" -eq 1 ]]; then
    echo ">> Setting console boot (kiosk), desktop available on demand ..."
    if command -v raspi-config >/dev/null 2>&1; then
        raspi-config nonint do_boot_behaviour B4 || true   # set up desktop + autologin
    fi
    systemctl set-default multi-user.target || true        # ...but boot to the kiosk
else
    echo ">> --no-boot-change: leaving the boot behaviour unchanged."
    echo "   NOTE: the desktop session must NOT be running at boot, or it will own"
    echo "         the display before the kiosk can. Set console boot via raspi-config."
fi

echo
echo ">> Done. Reboot to launch the panel:  sudo reboot"
echo "   Logs:       journalctl -u cncctl-kiosk -f"
echo "   Restart:    sudo systemctl restart cncctl-kiosk"
echo "   To desktop: tap 'Quit -> Desktop' (or: sudo systemctl stop cncctl-kiosk && sudo systemctl start lightdm)"
echo "   Back to it: sudo systemctl isolate multi-user.target"
echo "   Remove:     sudo deploy/uninstall.sh"
