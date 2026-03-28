# Bodhi Update Manager

![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![GTK](https://img.shields.io/badge/GTK-3.x-green)
![License](https://img.shields.io/github/license/flux-abyss/bodhi-update-manager)
![Status](https://img.shields.io/badge/status-stable-brightgreen)

A lightweight graphical update manager for **Bodhi Linux**.

Built with **Python**, **GTK3**, and **VTE**, the application provides a unified interface for managing updates across supported package systems while preserving terminal output during install operations.

---

## Screenshots

<p align="center">
  <a href="doc/screenshots/bodhi-update-manager-1.png">
    <img src="doc/screenshots/bodhi-update-manager-1.png" width="48%">
  </a>
  <a href="doc/screenshots/bodhi-update-manager-2.png">
    <img src="doc/screenshots/bodhi-update-manager-2.png" width="48%">
  </a>
</p>

---

## Key Concepts

- **Dynamic plugin discovery**  
  Backends are discovered at runtime by scanning `bodhi_update/plugins/`.

- **No hardcoded backend imports**  
  Backend registration is handled dynamically.

- **UI is backend-agnostic**  
  Aggregates updates from all active backends into one interface.

- **Strict privilege separation**  
  Only operations that require elevation use the helper/polkit path.

- **Debian-first packaging**  
  Installation layout is controlled by Debian packaging.

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

## Build

```bash
dpkg-buildpackage -us -uc -b
Install
sudo dpkg -i ../bodhi-update-manager_*.deb
Run
bodhi-update-manager
```
## Dependencies
```
python3
python3-gi
gir1.2-gtk-3.0
gir1.2-vte-2.91
python3-apt
```
## Optional:
```
flatpak
snapd
```
## License:
GPL-3.0-or-later
