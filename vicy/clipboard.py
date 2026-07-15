"""Clipboard helper: wl-copy first, GTK clipboard as fallback."""

import subprocess


def copy_to_clipboard(text: str) -> bool:
    try:
        subprocess.run(["wl-copy"], input=text.encode(), check=True, timeout=5)
        return True
    except Exception:
        try:
            from gi.repository import Gdk, Gtk

            cb = Gtk.Clipboard.get_default(Gdk.Display.get_default())
            cb.set_text(text, -1)
            cb.store()
            return True
        except Exception:
            return False
