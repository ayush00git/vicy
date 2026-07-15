"""Unix-socket IPC between the CLI verbs and the running instance.

`send_command` is stdlib-only so the hotkey fast path (`--toggle`) never
pays for GTK/numpy imports. `IpcServer` runs inside the GTK main loop.
"""

import os
import socket

from .config import SOCK_PATH


def send_command(cmd: str) -> str:
    """Send a command to the running Vicy instance and return its reply."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(SOCK_PATH)
        s.sendall(cmd.encode())
        return s.recv(256).decode().strip()
    finally:
        s.close()


class IpcServer:
    """Listens on SOCK_PATH; dispatches commands on the GTK main loop.

    `handler` receives the command string and returns the reply string.
    """

    def __init__(self, handler):
        from gi.repository import GLib  # lazy: keep CLI imports light

        self._handler = handler
        try:
            os.unlink(SOCK_PATH)
        except FileNotFoundError:
            pass
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(SOCK_PATH)
        self._srv.listen(2)
        GLib.io_add_watch(self._srv.fileno(), GLib.IO_IN, self._on_ready)

    def _on_ready(self, _fd, _cond):
        try:
            conn, _ = self._srv.accept()
            conn.settimeout(1)
            cmd = conn.recv(64).decode().strip()
            conn.sendall(self._handler(cmd).encode())
            conn.close()
        except OSError:
            pass
        return True

    @staticmethod
    def cleanup():
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
