"""Inject the transcript into the focused app.

Preference order:
1. ydotool (if its daemon socket exists) — types real characters, so it
   also works in terminals.
2. XDG RemoteDesktop portal — injects Ctrl+V after the transcript was
   copied to the clipboard. Native GNOME Wayland path; asks for
   permission once and persists it with a restore token.
3. Nothing — the transcript is on the clipboard anyway.
"""

import os
import shutil
import subprocess
import time

from gi.repository import Gio, GLib

from . import config

KEY_LEFTCTRL = 29  # linux evdev keycodes
KEY_V = 47
KEYBOARD = 1  # RemoteDesktop device bitmask
PERSIST = 2   # keep permission until explicitly revoked

TOKEN_FILE = os.path.join(config.CACHE_DIR, "portal-token")


def _ydotool_socket():
    path = os.environ.get("YDOTOOL_SOCKET", "/tmp/.ydotool_socket")
    return path if os.path.exists(path) else None


class PortalPaster:
    """Keyboard injection through org.freedesktop.portal.RemoteDesktop.

    The session handshake (CreateSession → SelectDevices → Start) is
    asynchronous over DBus Request/Response signals and runs once at
    startup; paste() then fires Ctrl+V key events synchronously.
    """

    def __init__(self):
        self._bus = None
        self._sender = None
        self._session = None
        self.ready = False
        self._counter = 0

    def prepare(self):
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._sender = self._bus.get_unique_name()[1:].replace(".", "_")
            self._create_session()
        except Exception:
            self._bus = None

    # -- Request/Response plumbing ------------------------------------

    def _dbus_call(self, method, params):
        return self._bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.RemoteDesktop",
            method,
            params,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )

    def _opts(self, on_response):
        """Build an options dict whose handle_token routes the portal's
        Response signal to `on_response(code, results)`."""
        self._counter += 1
        token = f"vicyreq{self._counter}"
        path = f"/org/freedesktop/portal/desktop/request/{self._sender}/{token}"
        sub = {}

        def cb(_bus, _sender, _path, _iface, _signal, params):
            self._bus.signal_unsubscribe(sub["id"])
            code, results = params.unpack()
            on_response(code, results)

        sub["id"] = self._bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            path,
            None,
            Gio.DBusSignalFlags.NONE,
            cb,
        )
        return {"handle_token": GLib.Variant("s", token)}

    # -- Session handshake ---------------------------------------------

    def _create_session(self):
        def on_response(code, results):
            if code != 0 or "session_handle" not in results:
                return
            self._session = results["session_handle"]
            self._select_devices()

        opts = self._opts(on_response)
        opts["session_handle_token"] = GLib.Variant("s", "vicy_session")
        self._dbus_call("CreateSession", GLib.Variant("(a{sv})", (opts,)))

    def _select_devices(self):
        def on_response(code, _results):
            if code != 0:
                self._session = None
                return
            self._start()

        opts = self._opts(on_response)
        opts["types"] = GLib.Variant("u", KEYBOARD)
        opts["persist_mode"] = GLib.Variant("u", PERSIST)
        token = self._load_token()
        if token:
            opts["restore_token"] = GLib.Variant("s", token)
        self._dbus_call(
            "SelectDevices", GLib.Variant("(oa{sv})", (self._session, opts))
        )

    def _start(self):
        def on_response(code, results):
            if code != 0:
                self._session = None
                return
            if results.get("restore_token"):
                self._save_token(results["restore_token"])
            self.ready = True

        opts = self._opts(on_response)
        self._dbus_call(
            "Start", GLib.Variant("(osa{sv})", (self._session, "", opts))
        )

    @staticmethod
    def _load_token():
        try:
            with open(TOKEN_FILE) as f:
                return f.read().strip() or None
        except OSError:
            return None

    @staticmethod
    def _save_token(token):
        try:
            os.makedirs(config.CACHE_DIR, exist_ok=True)
            with open(TOKEN_FILE, "w") as f:
                f.write(token)
        except OSError:
            pass

    # -- Injection -------------------------------------------------------

    def paste(self) -> bool:
        """Send Ctrl+V to the focused window. Returns success."""
        if not (self.ready and self._session):
            return False
        try:
            for key, state in (
                (KEY_LEFTCTRL, 1),
                (KEY_V, 1),
                (KEY_V, 0),
                (KEY_LEFTCTRL, 0),
            ):
                self._dbus_call(
                    "NotifyKeyboardKeycode",
                    GLib.Variant("(oa{sv}iu)", (self._session, {}, key, state)),
                )
                time.sleep(0.01)
            return True
        except Exception:
            self.ready = False
            return False


class Typer:
    """Best-effort text injection with graceful degradation."""

    def __init__(self):
        self._portal = None
        if _ydotool_socket() is None:
            self._portal = PortalPaster()
            self._portal.prepare()

    def inject(self, text: str) -> str:
        """Deliver `text` to the focused app. The caller has already put
        it on the clipboard. Returns the method used:
        'type' | 'paste' | 'clipboard'."""
        sock = _ydotool_socket()
        if sock and shutil.which("ydotool"):
            try:
                subprocess.run(
                    ["ydotool", "type", "--key-delay", "2", "--", text],
                    env={**os.environ, "YDOTOOL_SOCKET": sock},
                    check=True,
                    timeout=30,
                )
                return "type"
            except Exception:
                pass
        if self._portal is not None and self._portal.paste():
            return "paste"
        return "clipboard"
