#!/usr/bin/env python3
"""Vicy — a floating Whisper voice-to-text widget.

Click the mic, speak, click again. The transcript slides out below the
pill and is copied to your clipboard. Drag the pill anywhere to move it;
right-click for model options and quit.
"""

import os

# GNOME on Wayland doesn't let native Wayland windows set keep-above,
# but honors it for XWayland windows — so force the X11 backend.
os.environ.setdefault("GDK_BACKEND", "x11")

import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
MODELS = ("tiny", "base", "small", "medium")
DEFAULT_MODEL = os.environ.get("VICY_MODEL", "base")
MIN_SECONDS = 0.3
LEVEL_BLOCKS = "▁▂▃▄▅▆▇█"

CSS = b"""
#vicywin { background: transparent; }
#pill {
    background: rgba(24, 24, 30, 0.94);
    border-radius: 24px;
    border: 1px solid rgba(255, 255, 255, 0.12);
}
#pill label { color: #e8e8ee; font-size: 12px; }
#transcript { color: #d8d8e0; font-size: 13px; }
#pill button {
    background: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
    padding: 2px 8px;
}
#mic { font-size: 20px; border-radius: 999px; }
#mic.recording { background: rgba(220, 60, 70, 0.30); }
#closebtn { font-size: 11px; color: #9a9aa4; }
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


class Vicy(Gtk.Window):
    def __init__(self):
        super().__init__(title="Vicy")
        self.state = "idle"  # idle | recording | transcribing
        self.model_name = DEFAULT_MODEL
        self._model = None
        self._model_lock = threading.Lock()
        self._stream = None
        self._chunks = []
        self._rms = 0.0
        self._rec_t0 = 0.0
        self.last_text = ""

        self._build_window()
        self._load_model_async(self.model_name)

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

        pill = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pill.set_name("pill")
        for side in ("top", "bottom", "start", "end"):
            getattr(pill, f"set_margin_{side}")(0)
        pill.set_border_width(8)
        self.add(pill)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pill.pack_start(row, False, False, 0)

        self.mic_btn = Gtk.Button(label="🎤")
        self.mic_btn.set_name("mic")
        self.mic_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.mic_btn.set_tooltip_text("Start/stop recording")
        self.mic_btn.connect("clicked", self._on_mic)
        row.pack_start(self.mic_btn, False, False, 0)

        self.status = Gtk.Label(label="Starting…")
        self.status.set_xalign(0.0)
        self.status.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.status.set_size_request(210, -1)
        row.pack_start(self.status, True, True, 0)

        close = Gtk.Button(label="✕")
        close.set_name("closebtn")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Quit Vicy")
        close.connect("clicked", lambda *_: Gtk.main_quit())
        row.pack_end(close, False, False, 0)

        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.transcript = Gtk.Label(label="")
        self.transcript.set_name("transcript")
        self.transcript.set_line_wrap(True)
        self.transcript.set_max_width_chars(42)
        self.transcript.set_selectable(True)
        self.transcript.set_xalign(0.0)
        self.transcript.set_margin_start(8)
        self.transcript.set_margin_end(8)
        self.transcript.set_margin_bottom(4)
        self.revealer.add(self.transcript)
        pill.pack_start(self.revealer, False, False, 0)

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self._on_press)
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", Gtk.main_quit)

        # Park near the top-center of the primary monitor.
        display = Gdk.Display.get_default()
        mon = display.get_primary_monitor() or display.get_monitor(0)
        geo = mon.get_geometry()
        self.show_all()
        w, _ = self.get_size()
        self.move(geo.x + (geo.width - w) // 2, geo.y + 48)

    def _on_press(self, _w, event):
        if event.button == 1:
            self.begin_move_drag(
                event.button, int(event.x_root), int(event.y_root), event.time
            )
            return True
        if event.button == 3:
            self._popup_menu(event)
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
        self.status.set_markup(markup)
        return False  # usable directly with GLib.idle_add

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
                        self._set_status, f"Ready (<b>{name}</b>) — click the mic"
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

    def _on_mic(self, _btn):
        if self.state == "idle":
            self._start_recording()
        elif self.state == "recording":
            self._stop_recording()

    def _start_recording(self):
        self._chunks = []
        self._rms = 0.0

        def audio_cb(indata, _frames, _time, _status):
            self._chunks.append(indata.copy())
            self._rms = float(np.sqrt(np.mean(indata**2)))

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
        self._rec_t0 = time.time()
        self.mic_btn.set_label("⏹")
        self.mic_btn.get_style_context().add_class("recording")
        self.revealer.set_reveal_child(False)
        GLib.timeout_add(120, self._tick)

    def _tick(self):
        if self.state != "recording":
            return False
        secs = int(time.time() - self._rec_t0)
        level = LEVEL_BLOCKS[min(int(self._rms * 40), len(LEVEL_BLOCKS) - 1)]
        self._set_status(
            f'<span foreground="#ff6b6b">●</span> {secs // 60}:{secs % 60:02d}'
            f"  {level}  recording…"
        )
        return True

    def _stop_recording(self):
        self.state = "transcribing"
        self.mic_btn.set_label("🎤")
        self.mic_btn.get_style_context().remove_class("recording")
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
            self.state = "idle"
            self._set_status("Too short — try again")
            return

        self.mic_btn.set_sensitive(False)
        self._set_status("Transcribing…")
        threading.Thread(
            target=self._transcribe, args=(audio,), daemon=True
        ).start()

    # ---------- Transcription ----------

    def _transcribe(self, audio):
        t0 = time.time()
        model = self._wait_model()
        if model is None:
            GLib.idle_add(self._finish, None, 0.0, 0.0)
            return
        try:
            segments, _info = model.transcribe(
                audio, beam_size=5, vad_filter=True
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            GLib.idle_add(self._set_status, f"Error: {exc}")
            GLib.idle_add(self._reset_mic)
            return
        GLib.idle_add(
            self._finish, text, len(audio) / SAMPLE_RATE, time.time() - t0
        )

    def _reset_mic(self):
        self.state = "idle"
        self.mic_btn.set_sensitive(True)
        return False

    def _finish(self, text, audio_secs, took):
        self._reset_mic()
        if text is None:
            self._set_status("Model not loaded — try again")
            return False
        if not text:
            self._set_status("No speech detected")
            return False
        self.last_text = text
        copied = copy_to_clipboard(text)
        clip = "copied to clipboard" if copied else "clipboard failed"
        self._set_status(
            f'<span foreground="#7bd88f">✓</span> {audio_secs:.0f}s audio '
            f"in {took:.1f}s — {clip}"
        )
        self.transcript.set_text(text)
        self.revealer.set_reveal_child(True)
        return False


def main():
    app = Vicy()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, Gtk.main_quit)  # SIGINT
    app.present()
    Gtk.main()


if __name__ == "__main__":
    main()
