"""GNOME global-hotkey registration.

Wayland apps can't grab global keys themselves, so we register a GNOME
custom keyboard shortcut that runs `run.sh --toggle`, which pokes the
running instance over the IPC socket.
"""

import os
import sys

from .config import APP_DIR


def _toggle_command() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller binary
        return f"{sys.executable} --toggle"
    return os.path.join(APP_DIR, "run.sh") + " --toggle"


def install_hotkey(binding: str) -> None:
    """Register a GNOME custom shortcut that toggles recording."""
    from gi.repository import Gio

    base = "org.gnome.settings-daemon.plugins.media-keys"
    path = f"/{base.replace('.', '/')}/custom-keybindings/vicy/"
    settings = Gio.Settings.new(base)
    paths = list(settings.get_strv("custom-keybindings"))
    if path not in paths:
        paths.append(path)
        settings.set_strv("custom-keybindings", paths)
    kb = Gio.Settings.new_with_path(f"{base}.custom-keybinding", path)
    kb.set_string("name", "Vicy: toggle recording")
    kb.set_string("command", _toggle_command())
    kb.set_string("binding", binding)
    Gio.Settings.sync()
    print(f"Registered GNOME shortcut: {binding} → toggle Vicy recording")
