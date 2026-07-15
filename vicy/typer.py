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
import sys
import time

from gi.repository import Gio, GLib

from . import config


def _log(*args):
    print("[vicy.typer]", *args, file=sys.stderr, flush=True)

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
        self._busy = False
        self._counter = 0
        self._on_done = None

    def prepare(self):
        """Start (or restart) the session handshake. Safe to call again
        after a failure — e.g. a dismissed permission dialog."""
        if self._busy or self.ready:
            return
        self._busy = True
        self._session = None
        try:
            if self._bus is None:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                self._sender = self._bus.get_unique_name()[1:].replace(".", "_")
            self._create_session()
        except Exception as exc:
            _log("portal unavailable:", exc)
            self._bus = None
            self._busy = False

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
                _log("CreateSession failed, code", code)
                self._busy = False
                self._finish_paste(False)
                return
            self._session = results["session_handle"]
            self._select_devices()

        opts = self._opts(on_response)
        self._counter += 1
        opts["session_handle_token"] = GLib.Variant(
            "s", f"vicy_session{self._counter}"
        )
        self._dbus_call("CreateSession", GLib.Variant("(a{sv})", (opts,)))

    def _select_devices(self):
        def on_response(code, _results):
            if code != 0:
                _log("SelectDevices failed, code", code)
                self._session = None
                self._busy = False
                self._finish_paste(False)
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
            self._busy = False
            if code != 0:
                _log("Start failed (dialog dismissed?), code", code)
                self._session = None
                self._finish_paste(False)
                return
            if results.get("restore_token"):
                self._save_token(results["restore_token"])
            self.ready = True
            _log("portal session ready, devices:", results.get("devices"))
            if self._on_done is not None:
                self._fire()

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

    def paste_async(self, on_done):
        """Open a transient session (silent thanks to the restore token),
        fire Ctrl+V at the focused window, then close the session so
        GNOME's remote-control indicator only blinks for the paste
        instead of living in the top bar. `on_done(ok)` reports the
        outcome (False → caller should tell the user to paste manually)."""
        self._on_done = on_done
        if self.ready:
            self._fire()
            return
        self.prepare()
        if self._bus is None:  # portal completely unavailable
            self._finish_paste(False)
            return

        def timeout():
            if self._on_done is not None:
                _log("paste timed out waiting for permission")
                self._finish_paste(False)
            return False

        GLib.timeout_add_seconds(30, timeout)

    def _fire(self):
        ok = self._send_ctrl_v()
        GLib.timeout_add(100, self._close_session)
        self._finish_paste(ok)

    def _finish_paste(self, ok):
        cb, self._on_done = self._on_done, None
        if cb is not None:
            cb(ok)

    def _close_session(self):
        if self._session is not None:
            try:
                self._bus.call_sync(
                    "org.freedesktop.portal.Desktop",
                    self._session,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
            except Exception as exc:
                _log("session close failed:", exc)
        self._session = None
        self.ready = False
        return False  # one-shot when scheduled via timeout_add

    def _send_ctrl_v(self) -> bool:
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
        except Exception as exc:
            _log("key injection failed:", exc)
            self.ready = False
            return False


class Typer:
    """Best-effort text injection with graceful degradation."""

    def __init__(self):
        # Sessions are opened per paste, so nothing to prepare up front.
        self._portal = None if _ydotool_socket() else PortalPaster()

    def inject(self, text: str, on_fallback=None):
        """Deliver `text` to the focused app. ydotool types it directly;
        the clipboard is only touched on the portal/manual fallback paths
        (which paste). `on_fallback()` is invoked (possibly async) if the
        user will have to paste manually."""
        sock = _ydotool_socket()
        if sock and shutil.which("ydotool"):
            try:
                subprocess.run(
                    ["ydotool", "type", "--key-delay", "2", "--", text],
                    env={**os.environ, "YDOTOOL_SOCKET": sock},
                    check=True,
                    timeout=30,
                )
                return
            except Exception as exc:
                _log("ydotool failed:", exc)
        from .clipboard import copy_to_clipboard

        copy_to_clipboard(text)  # fallback paths deliver via paste
        if self._portal is not None:
            self._portal.paste_async(
                lambda ok: (not ok and on_fallback and on_fallback())
            )
            return
        if on_fallback is not None:
            on_fallback()
