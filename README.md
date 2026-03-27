# Bodhi Update Manager

![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![GTK](https://img.shields.io/badge/GTK-3.x-green)
![License](https://img.shields.io/github/license/flux-abyss/bodhi-update-manager)
![Status](https://img.shields.io/badge/status-stable-brightgreen)

A lightweight graphical update manager for **Bodhi Linux**.

Built with **Python**, **GTK3**, and **VTE**, the application provides a unified interface for managing updates across supported package systems while preserving terminal output during install operations.

> Status: **v1.2.0 — Localization/Debian Trixie support**

---

## Tested Environment

| Component | Version |
|----------|--------|
| OS | Bodhi Linux (Ubuntu/Debian base) |
| Desktop | Moksha |
| Python | 3.10+ |
| GUI toolkit | GTK3 (PyGObject) |
| Terminal | VTE |

---

## Current Architecture

The project uses a **plugin-based backend system** with a **Debian-controlled install layout**.

```
bodhi-update-manager/
├── data/
│   ├── applications/
│   │   └── bodhi-update-manager.desktop
│   ├── icons/
│   │   └── bodhi-update-manager.png
│   ├── libexec/
│   │   └── bodhi-update-manager-root
│   ├── polkit/
│   │   └── org.bodhi.updatemanager.policy
│   └── systemd/
│       ├── bodhi-update-manager-refresh.service
│       └── bodhi-update-manager-refresh.timer
├── debian/
├── pyproject.toml
├── README.md
└── src/
    └── bodhi_update/
        ├── __init__.py
        ├── app.py
        ├── backends.py
        ├── install_commands.py
        ├── main.py
        ├── models.py
        ├── utils.py
        └── plugins/
            ├── __init__.py
            ├── apt.py
            ├── flatpak.py
            └── snap.py
```

---

## Key Concepts

- **Dynamic plugin discovery**  
  Backends are discovered at runtime by scanning `bodhi_update/plugins/`.

- **No hardcoded backend imports**  
  Backend registration is handled dynamically through `discover_plugins()`.

- **UI is backend-agnostic**  
  `app.py` aggregates updates from all active backends into one interface.

- **Strict privilege separation**  
  Only operations that require elevation use the helper/polkit path.

- **Debian-first packaging**  
  Installation layout is controlled by Debian packaging, not setuptools or pybuild.

---

## Backend System

Backends live under:

```
src/bodhi_update/plugins/
```

A plugin is loaded if:

- the module exists
- it imports successfully
- it defines a valid `UpdateBackend` subclass

Currently shipped backends:

- APT
- Flatpak
- Snap

---

## Available Backends

### APT
- Uses `python-apt`
- Primary system package backend
- Uses helper + polkit for privileged operations

### Flatpak
- Detects Flatpak updates
- No root required for standard operations

### Snap
- Optional backend
- Loaded only if Snap environment is present

---

## Background Refresh

The package includes:

- `bodhi-update-manager-refresh.service`
- `bodhi-update-manager-refresh.timer`

---

## Privilege Model

| Operation | Method |
|----------|--------|
| APT install/update | helper + polkit |
| Background refresh | systemd |
| Flatpak / Snap | native tools |
| UI usage | normal user |

No blanket sudo usage.

---

## Root Helper

Installed at:

```
/usr/libexec/bodhi-update-manager-root
```

Used only for privileged APT operations.

---

## UI Overview

### Main Window
- Refresh
- Install Selected
- Selection controls
- Unified update list
- Status display
- Embedded terminal (VTE)

### Behavior
- Aggregates updates from all backends
- Preserves terminal output during installs
- Handles partial backend failures gracefully

---

## Install Workflow

### Refresh
- Queries all backends
- Aggregates results

### Install
- Commands built via `install_commands.py`
- Executed inside VTE terminal
- Full output visible

---

## Packaging and Install Layout

Installed layout:

```
/usr/bin/bodhi-update-manager
/usr/lib/bodhi-update-manager/bodhi_update/
/usr/libexec/bodhi-update-manager-root
/usr/share/applications/bodhi-update-manager.desktop
/usr/share/icons/hicolor/256x256/apps/bodhi-update-manager.png
/usr/share/polkit-1/actions/org.bodhi.updatemanager.policy
/usr/lib/systemd/system/bodhi-update-manager-refresh.service
/usr/lib/systemd/system/bodhi-update-manager-refresh.timer
```

---

## Build

```bash
dpkg-buildpackage -us -uc -b
```

---

## Install

```bash
sudo dpkg -i ../bodhi-update-manager_*.deb
```

---

## Run

```bash
bodhi-update-manager
```

---

## Dependencies

Core:

```bash
python3
python3-gi
gir1.2-gtk-3.0
gir1.2-vte-2.91
python3-apt
```

Optional:

```bash
flatpak
snapd
```

---

## Current Status

- Debian package builds successfully
- Application installs and runs correctly
- Dynamic plugin discovery works
- APT backend verified working
- Flatpak backend verified working

---

## Tray Icon & Startup (Bodhi / Moksha)

Bodhi Update Manager includes an optional system tray icon that can be launched manually:

```bash
bodhi-update-manager-tray
```

---

## Design Philosophy

- Lightweight
- Modular
- Explicit
- Backend-agnostic
- Debian-friendly

---

## Notes

This is a frontend for update workflows, not a replacement for package managers.

---

## Credits

Joseph Wiley (Flux-Abyss)

---

## License

GPL-3.0-or-later
