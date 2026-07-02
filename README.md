# 🛠️ CHAARE /t͡ʃaː.ˈra/ — CNC Retrofitting!

[![Hardware](https://img.shields.io/badge/Hardware-Open_Source-orange.svg)]()
[![Status](https://img.shields.io/badge/Status-Active-brightgreen.svg)]()

A project to revive and modernize two obsolete CNC machines — the **EMCO F1-CNC
Mill** and the **Light Machines Corporation spectraLIGHT Lathe** — by replacing
their original controllers with an open-source stack: a Raspberry Pi Pico
running grblHAL firmware, driven by a custom Python control application.

## The stack

```
GUI / G-code ─► cncctl (Python, USB-CDC) ─► grblHAL (Pico / RP2040) ─► stepper drivers ─► EMCO mill / spectraLIGHT lathe
```

## Repository layout

| Directory | Contents |
|-----------|----------|
| **[`software/`](software/)** | Host-side control software (Python) — the `cncctl` core library and the operator GUI. |
| **[`grbl/`](grbl/)** | grblHAL firmware for the RP2040 Pico: board configuration, reproducible build scripts, and a prebuilt `.uf2`. |
| **[`docs/`](docs/)** | Hardware manuals and datasheets for the machines and electronics. |
| **[`admin/`](admin/)** | Project-management artifacts (schedule, progress reports). |

## Getting started

- **Run the operator GUI** → [`software/pitrofit/`](software/pitrofit/README.md)
- **Build / flash the firmware** → [`grbl/`](grbl/README.md)
- **Machine manuals** → [`docs/`](docs/README.md)
