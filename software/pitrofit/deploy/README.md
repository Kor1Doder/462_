# Deploying cncctl on a Raspberry Pi 5

Turn a Raspberry Pi 5 into a dedicated CNC operator panel that boots into the
desktop and launches the cncctl GUI fullscreen, connected to the grblHAL Pico
over USB.

## Target

- **Board:** Raspberry Pi 5 (works on a Pi 4 too).
- **OS:** Raspberry Pi OS *Trixie* (Debian 13), **64-bit**. The 64-bit image is
  required — the Qt/numpy/matplotlib wheels we install are aarch64. We run our
  own minimal **labwc** kiosk (Trixie dropped `cage`/`weston`/`wayfire`, so
  labwc is the only packaged compositor); a Desktop image is fine but the
  installer switches it to console boot so the kiosk owns the display.
- **Display:** any HDMI monitor (a touchscreen works well for the panel).
- **Link:** the grblHAL Pico on USB, appearing as `/dev/ttyACM0`.

## One-command install

```bash
sudo apt update && sudo apt install -y git
git clone <this-repo-url> ~/cncctl
cd ~/cncctl
sudo deploy/install.sh
sudo reboot
```

After reboot the Pi comes up directly in the operator panel, **fullscreen**,
with no desktop or taskbar — a dedicated labwc kiosk run by systemd shows only
the GUI. If the GUI closes or crashes it relaunches within ~2 s; if labwc itself
dies, systemd restarts it.

### What the installer does

1. `apt`-installs `labwc` + the Qt6 / Wayland / OpenGL runtime libraries.
2. Creates a Python venv at `~/cncctl/.venv` and installs `requirements.txt`
   (all aarch64 wheels — no compilation) plus the `cncctl` package itself.
   Trixie ships Python 3.13, which satisfies our `>=3.12`; on an older image it
   provisions 3.12 with [`uv`](https://docs.astral.sh/uv/).
3. Adds your user to `dialout` (serial), `render`/`video` (GPU), and `input`.
4. Installs a udev rule giving the Pico a stable `/dev/grblhal` symlink.
5. Installs the **`cncctl-kiosk`** systemd service (a minimal labwc on VT1 with
   an isolated config at `~/.cncctl-kiosk/`) and switches the Pi to console boot
   so the kiosk owns the display.

Flags: `--no-boot-change` leaves the boot behaviour alone (you must then disable
the desktop yourself); `--user <name>` installs for a specific account (default:
the `sudo` user).

## Day-to-day

```bash
journalctl -u cncctl-kiosk -f          # live logs
sudo systemctl restart cncctl-kiosk    # restart the panel
sudo systemctl disable --now cncctl-kiosk   # drop the kiosk entirely
```

The panel is launched by `deploy/start-kiosk.sh`, which relaunches the GUI in a
loop so the appliance is always up. To pause it for maintenance:

```bash
touch ~/.cncctl-stop                   # the relaunch loop stops after you close the GUI
# ... switch to a console with Ctrl-Alt-F2, or SSH in ...
rm ~/.cncctl-stop && sudo systemctl restart cncctl-kiosk
```

### Quit to the desktop (debugging)

The GUI's **"Quit → Desktop"** button (top bar, with a confirmation) closes the
panel and switches the Pi into its normal **desktop session** — handy for
debugging. A root-owned helper (`/usr/local/sbin/cncctl-to-desktop`, run via a
scoped `NOPASSWD` sudoers rule) **stops the kiosk and starts `lightdm`**, which
autologins (`autologin-user` in `/etc/lightdm/lightdm.conf`, set up by the
installer). It deliberately does *not* `isolate graphical.target` — that target
includes `multi-user.target`, so the kiosk would keep running and hold the seat,
leaving lightdm stuck at the password greeter.

A user-demanded quit **stays closed for the session**: it drops a sentinel
(`~/.cncctl-stop`) that the relaunch loop honours, so the panel does *not* pop
back up. (A crash or a frozen-then-killed GUI leaves no sentinel and relaunches
as normal — that's the distinction.) The sentinel is cleared automatically when
the kiosk service next starts, so a reboot or an explicit start brings it back.

To go **back to the kiosk** from a desktop terminal:

```bash
sudo systemctl isolate multi-user.target   # stops lightdm, starts the kiosk
```

Quit does **not** stop the machine — the hardwired e-stop is your real stop.

## Configure the machine

Edit [`config/machine.toml`](../config/machine.toml) before cutting anything —
it ships with **placeholder zeros** on purpose. Fill in the
calibrated steps/mm, rates, accelerations, and soft limits, and set the serial
port. If you installed the udev rule, prefer the stable name:

```toml
[transport]
default_port = "/dev/grblhal"
```

## Verifying the USB link

```bash
ls -l /dev/ttyACM0 /dev/grblhal      # device present, group "dialout"
groups                               # you should be in "dialout"
```

If the port is missing, check the USB cable and `dmesg | tail`. If permission is
denied, log out/in once so the new group membership takes effect.

## Updating

```bash
cd ~/cncctl
git pull
.venv/bin/python -m pip install -r requirements.txt   # if deps changed
touch ~/.cncctl-stop && sleep 3 && rm ~/.cncctl-stop   # bounce the panel
```

## Uninstall

```bash
sudo deploy/uninstall.sh
```

Removes the autostart entry and the udev rule (leaves the repo, venv, and group
memberships). Boot behaviour is left unchanged — adjust with `sudo raspi-config`.

## Notes on performance

- Everything installs from prebuilt **aarch64 wheels**, so the Pi never
  compiles numpy/Qt/matplotlib.
- The GUI carves the workpiece heightmap off the UI thread; the 3D view uses the
  Pi 5 V3D GPU through Mesa, so orbiting stays smooth.
- Running on the existing labwc session avoids a second compositor — the panel
  is just one fullscreen Wayland client.
