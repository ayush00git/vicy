#!/usr/bin/env python3
"""Vicy — a floating Whisper voice-to-text widget.

A single pill of waveform bars. Click it (or press the global hotkey),
speak — the bars dance with your voice's frequency spectrum — click
again, and the transcript slides out below and lands in your clipboard.
Drag to move, right-click for models and quit.
"""

import os

# GNOME on Wayland doesn't let native Wayland windows set keep-above,
# but honors it for XWayland windows — so force the X11 backend.
os.environ.setdefault("GDK_BACKEND", "x11")

import atexit
import math
import socket
import subprocess
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SOCK_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "vicy.sock")
HOTKEY_DEFAULT = "<Control>m"


def send_command(cmd: str) -> str:
    """Send a command to the running Vicy instance over its unix socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(SOCK_PATH)
        s.sendall(cmd.encode())
        return s.recv(256).decode().strip()
    finally:
        s.close()


def install_hotkey(binding: str) -> None:
    """Register a GNOME custom shortcut that runs `vicy.py --toggle`."""
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
    kb.set_string("command", os.path.join(APP_DIR, "run.sh") + " --toggle")
    kb.set_string("binding", binding)
    Gio.Settings.sync()
    print(f"Registered GNOME shortcut: {binding} → toggle Vicy recording")


# Handle CLI verbs before the heavy imports below so the hotkey feels instant.
if __name__ == "__main__" and len(sys.argv) > 1:
    _arg = sys.argv[1]
    if _arg == "--toggle":
        try:
            print(send_command("toggle"))
            sys.exit(0)
        except OSError:
            pass  # not running — fall through and start the app instead
    elif _arg == "--status":
        try:
            print(send_command("status"))
        except OSError:
            print("not running")
        sys.exit(0)
    elif _arg == "--install-hotkey":
        install_hotkey(sys.argv[2] if len(sys.argv) > 2 else HOTKEY_DEFAULT)
        sys.exit(0)
    else:
        sys.exit(f"Unknown option: {_arg} (use --toggle | --status | --install-hotkey [binding])")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

import cairo
import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
MODELS = ("tiny", "base", "small", "medium")
DEFAULT_MODEL = os.environ.get("VICY_MODEL", "base")
MIN_SECONDS = 0.3
FPS_MS = 33          # ~30 fps animation while active
FFT_SIZE = 1024      # ~64 ms of audio per spectrum frame
BAND_LO, BAND_HI = 80.0, 4000.0  # voice range mapped across the bars
DRAG_THRESHOLD = 6   # px of motion before a click becomes a window drag

# Monochrome palette: the pill is near-black, bars are white with the
# grey shades coming from alpha over the dark background.
BAR_COLOR = (1.0, 1.0, 1.0)

CSS = b"""
#vicywin { background: transparent; }
#pill {
    background: rgba(24, 24, 30, 0.94);
    border-radius: 24px;
    border: 1px solid rgba(255, 255, 255, 0.12);
}
#pill label { color: #e8e8ee; font-size: 12px; }
#pill label#status { color: #9a9aa4; font-size: 10px; }
#pill label#transcript { color: #d8d8e0; font-size: 13px; }
"""


def copy_to_clipboard(text: str) -> bool:
    try:
        subprocess.run(["wl-copy"], input=text.encode(), check=True, timeout=5)
        return True
    except Exception:
        try:
            cb = Gtk.Clipboard.get_default(Gdk.Display.get_default())
            cb.set_text(text, -1)
            cb.store()
            return True
        except Exception:
            return False


class WaveView(Gtk.DrawingArea):
    """The pill's only face: a row of frequency bars.

    idle      — calm resting wave, always visible
    recording — each bar tracks one frequency band of the live mic
    busy      — a traveling pulse while Whisper transcribes
    """

    N_BARS = 36

    def __init__(self):
        super().__init__()
        self.mode = "idle"  # idle | recording | busy
        self.bars = np.zeros(self.N_BARS)
        self.phase = 0.0
        self.set_size_request(180, 48)
        self.connect("draw", self._draw)

    def set_mode(self, mode):
        self.mode = mode
        if mode != "recording":
            self.bars = np.zeros(self.N_BARS)
        self.queue_draw()

    def set_bars(self, vals):
        self.bars = vals
        self.queue_draw()

    def tick(self):
        self.phase += 0.16
        self.queue_draw()

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        cy = h / 2
        step = w / self.N_BARS
        cr.set_line_width(3)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        for i in range(self.N_BARS):
            x = step / 2 + i * step
            if self.mode == "recording":
                v = float(self.bars[i])
                amp = 0.08 + 0.88 * v
                alpha = 0.30 + 0.70 * v  # grey at rest, white on voice
            elif self.mode == "busy":
                amp = 0.25 + 0.20 * math.sin(self.phase - i * 0.55)
                alpha = 0.55
            else:  # idle: gentle frozen wave
                amp = 0.10 + 0.06 * math.sin(i * 0.7)
                alpha = 0.28
            bh = max(1.5, amp * (cy - 4))
            cr.set_source_rgba(*BAR_COLOR, alpha)
            cr.move_to(x, cy - bh)
            cr.line_to(x, cy + bh)
            cr.stroke()


class Vicy(Gtk.Window):
    def __init__(self):
        super().__init__(title="Vicy")
        self.state = "idle"  # idle | recording | transcribing
        self.model_name = DEFAULT_MODEL
        self._model = None
        self._model_lock = threading.Lock()
        self._stream = None
        self._chunks = []
        self._bars = np.zeros(WaveView.N_BARS)
        self._spec_peak = 3.0
        self._anim_id = None
        self._press_pos = None
        self._status_seq = 0
        self.last_text = ""

        # Precompute FFT band slices: log-spaced voice-range bands.
        freqs = np.fft.rfftfreq(FFT_SIZE, 1 / SAMPLE_RATE)
        edges = np.geomspace(BAND_LO, BAND_HI, WaveView.N_BARS + 1)
        self._band_masks = [
            (freqs >= lo) & (freqs < hi) for lo, hi in zip(edges[:-1], edges[1:])
        ]
        self._fft_window = np.hanning(FFT_SIZE)

        self._build_window()
        self._start_ipc_server()
        self._load_model_async(self.model_name)

    # ---------- IPC (global hotkey → --toggle → unix socket) ----------

    def _start_ipc_server(self):
        try:
            os.unlink(SOCK_PATH)
        except FileNotFoundError:
            pass
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(SOCK_PATH)
        self._srv.listen(2)
        GLib.io_add_watch(self._srv.fileno(), GLib.IO_IN, self._on_ipc)

    def _on_ipc(self, _fd, _cond):
        try:
            conn, _ = self._srv.accept()
            conn.settimeout(1)
            cmd = conn.recv(64).decode().strip()
            conn.sendall(self._handle_ipc(cmd).encode())
            conn.close()
        except OSError:
            pass
        return True

    def _handle_ipc(self, cmd):
        if cmd == "toggle":
            if self.state == "transcribing":
                return "busy transcribing"
            self._on_toggle()
            return self.state  # recording | transcribing
        if cmd == "status":
            return self.state
        if cmd == "show":
            self.present()
            return "ok"
        return "unknown command"

    # ---------- UI ----------

    def _build_window(self):
        self.set_name("vicywin")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.stick()
        self.set_app_paintable(True)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        pill = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        pill.set_name("pill")
        pill.set_border_width(6)
        self.add(pill)

        self.wave = WaveView()
        self.wave.set_tooltip_text("Click or Ctrl+M: record · drag to move")
        pill.pack_start(self.wave, True, True, 0)

        self.status = Gtk.Label(label="Starting…")
        self.status.set_name("status")
        self.status.set_xalign(0.5)
        self.status.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.status.set_no_show_all(True)
        self.status.show()
        pill.pack_start(self.status, False, False, 0)

        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.transcript = Gtk.Label(label="")
        self.transcript.set_name("transcript")
        self.transcript.set_line_wrap(True)
        self.transcript.set_max_width_chars(26)
        self.transcript.set_selectable(True)
        self.transcript.set_xalign(0.0)
        self.transcript.set_margin_start(10)
        self.transcript.set_margin_end(10)
        self.transcript.set_margin_top(4)
        self.transcript.set_margin_bottom(6)
        self.revealer.add(self.transcript)
        pill.pack_start(self.revealer, False, False, 0)

        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("button-press-event", self._on_press)
        self.connect("button-release-event", self._on_release)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", Gtk.main_quit)

        # Park near the top-center of the primary monitor.
        display = Gdk.Display.get_default()
        mon = display.get_primary_monitor() or display.get_monitor(0)
        geo = mon.get_geometry()
        self.show_all()
        w, _ = self.get_size()
        self.move(geo.x + (geo.width - w) // 2, geo.y + 48)

    # Click toggles recording; moving past a small threshold becomes a drag.

    def _on_press(self, _w, event):
        if event.button == 1:
            self._press_pos = (event.x_root, event.y_root)
            return True
        if event.button == 3:
            self._popup_menu(event)
            return True
        return False

    def _on_motion(self, _w, event):
        if self._press_pos is not None:
            dx = event.x_root - self._press_pos[0]
            dy = event.y_root - self._press_pos[1]
            if abs(dx) > DRAG_THRESHOLD or abs(dy) > DRAG_THRESHOLD:
                self._press_pos = None
                self.begin_move_drag(
                    1, int(event.x_root), int(event.y_root), event.time
                )
        return False

    def _on_release(self, _w, event):
        if event.button == 1 and self._press_pos is not None:
            self._press_pos = None
            self._on_toggle()
            return True
        return False

    def _on_key(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.revealer.set_reveal_child(False)
            return True
        return False

    def _popup_menu(self, event):
        menu = Gtk.Menu()
        group = None
        for name in MODELS:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(
                group, f"Model: {name}"
            )
            group = group or item
            item.set_active(name == self.model_name)
            item.connect("activate", self._on_model_pick, name)
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        copy_item = Gtk.MenuItem(label="Copy last transcript")
        copy_item.set_sensitive(bool(self.last_text))
        copy_item.connect(
            "activate", lambda *_: copy_to_clipboard(self.last_text)
        )
        menu.append(copy_item)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(quit_item)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _on_model_pick(self, item, name):
        if item.get_active() and name != self.model_name:
            self.model_name = name
            self._load_model_async(name)

    def _set_status(self, markup):
        if markup:
            self.status.set_markup(markup)
            self.status.show()
        else:
            self.status.hide()
        return False  # usable directly with GLib.idle_add

    def _flash_status(self, markup, secs=6):
        """Show a status line, then clear it unless something replaced it."""
        self._status_seq += 1
        seq = self._status_seq

        def clear():
            if self._status_seq == seq:
                self._set_status("")
            return False

        self._set_status(markup)
        GLib.timeout_add_seconds(secs, clear)
        return False

    # ---------- Model ----------

    def _load_model_async(self, name):
        with self._model_lock:
            self._model = None
        self._set_status(f"Loading <b>{name}</b> model…")

        def worker():
            try:
                from faster_whisper import WhisperModel

                model = WhisperModel(name, device="cpu", compute_type="int8")
            except Exception as exc:
                GLib.idle_add(self._set_status, f"Model error: {exc}")
                return
            with self._model_lock:
                if self.model_name == name:
                    self._model = model
                    GLib.idle_add(
                        self._flash_status, f"Ready (<b>{name}</b>) — Ctrl+M or click"
                    )

        threading.Thread(target=worker, daemon=True).start()

    def _wait_model(self, timeout=300):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._model_lock:
                if self._model is not None:
                    return self._model
            time.sleep(0.1)
        return None

    # ---------- Recording ----------

    def _on_toggle(self, *_):
        if self.state == "idle":
            self._start_recording()
        elif self.state == "recording":
            self._stop_recording()

    def _start_recording(self):
        self._chunks = []

        def audio_cb(indata, _frames, _time, _status):
            self._chunks.append(indata.copy())

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=audio_cb,
            )
            self._stream.start()
        except Exception as exc:
            self._set_status(f"Mic error: {exc}")
            return

        self.state = "recording"
        self._bars = np.zeros(WaveView.N_BARS)
        self._spec_peak = 3.0
        self.wave.set_mode("recording")
        self.revealer.set_reveal_child(False)
        self._set_status("")
        self._start_anim()

    def _spectrum(self):
        """Log-banded magnitude spectrum of the last ~64 ms of audio."""
        parts, have = [], 0
        for chunk in reversed(self._chunks):
            parts.append(chunk[:, 0])
            have += len(chunk)
            if have >= FFT_SIZE:
                break
        if not parts:
            return None
        x = np.concatenate(parts[::-1])[-FFT_SIZE:]
        if len(x) < FFT_SIZE:
            x = np.pad(x, (FFT_SIZE - len(x), 0))
        mag = np.abs(np.fft.rfft(x * self._fft_window))
        return np.array(
            [float(mag[m].mean()) if m.any() else 0.0 for m in self._band_masks]
        )

    def _start_anim(self):
        if self._anim_id is None:
            self._anim_id = GLib.timeout_add(FPS_MS, self._animate)

    def _animate(self):
        if self.state == "recording":
            bands = self._spectrum()
            if bands is not None:
                # Adaptive normalization with a floor so silence stays calm,
                # then fast-attack / slow-decay smoothing per bar.
                self._spec_peak = max(self._spec_peak * 0.99, float(bands.max()), 3.0)
                vals = np.clip(bands / self._spec_peak, 0.0, 1.0) ** 0.6
                self._bars = np.maximum(vals, self._bars * 0.72)
                self.wave.set_bars(self._bars)
            return True
        if self.state == "transcribing":
            self.wave.tick()
            return True
        self._anim_id = None
        return False

    def _stop_recording(self):
        self.state = "transcribing"
        self.wave.set_mode("busy")
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
        self._stream = None

        if self._chunks:
            audio = np.concatenate(self._chunks, axis=0).reshape(-1)
        else:
            audio = np.zeros(0, dtype="float32")
        self._chunks = []

        if len(audio) < MIN_SECONDS * SAMPLE_RATE:
            self._reset_idle()
            self._flash_status("Too short — try again", 4)
            return

        self._set_status("Transcribing…")
        threading.Thread(
            target=self._transcribe, args=(audio,), daemon=True
        ).start()

    # ---------- Transcription ----------

    def _transcribe(self, audio):
        t0 = time.time()
        model = self._wait_model()
        if model is None:
            GLib.idle_add(self._finish, None, 0.0, 0.0, 0.0)
            return
        audio = audio - float(audio.mean())  # remove DC offset
        peak = float(np.abs(audio).max()) if len(audio) else 0.0
        self._save_last(audio)
        if 0 < peak < 0.3:
            audio = audio * (0.9 / peak)  # rescue quiet captures
        try:
            segments, _info = model.transcribe(
                audio,
                beam_size=5,
                vad_filter=True,
                vad_parameters={"threshold": 0.35, "min_silence_duration_ms": 500},
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            GLib.idle_add(self._set_status, f"Error: {exc}")
            GLib.idle_add(self._reset_idle)
            return
        GLib.idle_add(
            self._finish, text, len(audio) / SAMPLE_RATE, time.time() - t0, peak
        )

    @staticmethod
    def _save_last(audio):
        """Keep the last capture at ~/.cache/vicy/last.wav for debugging."""
        try:
            import wave

            cache = os.path.expanduser("~/.cache/vicy")
            os.makedirs(cache, exist_ok=True)
            with wave.open(os.path.join(cache, "last.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
                w.writeframes(pcm.tobytes())
        except Exception:
            pass

    def _reset_idle(self):
        self.state = "idle"
        self.wave.set_mode("idle")
        return False

    def _finish(self, text, audio_secs, took, peak=0.0):
        self._reset_idle()
        if text is None:
            self._flash_status("Model not loaded — try again", 6)
            return False
        if not text:
            hint = " — mic muted or too quiet?" if peak < 0.05 else ""
            self._flash_status(
                f"No speech detected (mic peak {peak * 100:.0f}%){hint}", 8
            )
            return False
        self.last_text = text
        copied = copy_to_clipboard(text)
        clip = "copied to clipboard" if copied else "clipboard failed"
        self._flash_status(
            f"✓ {audio_secs:.0f}s audio in {took:.1f}s — {clip}", 8
        )
        self.transcript.set_text(text)
        self.revealer.set_reveal_child(True)
        return False


def main():
    try:
        send_command("show")
        print("Vicy is already running — brought to front.")
        return
    except OSError:
        pass

    app = Vicy()
    atexit.register(lambda: os.path.exists(SOCK_PATH) and os.unlink(SOCK_PATH))
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, Gtk.main_quit)  # SIGINT
    app.present()
    Gtk.main()


if __name__ == "__main__":
    main()
