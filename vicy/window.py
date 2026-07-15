"""The floating pill window: UI, state machine, and wiring."""

import atexit
import threading
import time

import numpy as np

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from . import config
from .audio import Recorder, SpectrumAnalyzer
from .clipboard import copy_to_clipboard
from .ipc import IpcServer, send_command
from .transcriber import Transcriber
from .wave_view import WaveView


class Vicy(Gtk.Window):
    def __init__(self):
        super().__init__(title="Vicy")
        self.state = "idle"  # idle | recording | transcribing
        self.model_name = config.DEFAULT_MODEL
        self.last_text = ""
        self._anim_id = None
        self._press_pos = None
        self._status_seq = 0

        self.recorder = Recorder()
        self.analyzer = SpectrumAnalyzer()
        self.transcriber = Transcriber()

        self._build_window()
        self._ipc = IpcServer(self._handle_ipc)
        self._load_model_async(self.model_name)

    # ---------- IPC (global hotkey → --toggle → unix socket) ----------

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
        provider.load_from_data(config.CSS)
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
            if abs(dx) > config.DRAG_THRESHOLD or abs(dy) > config.DRAG_THRESHOLD:
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
        for name in config.MODELS:
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
        self._set_status(f"Loading <b>{name}</b> model…")

        def done(error):
            if error:
                GLib.idle_add(self._set_status, f"Model error: {error}")
            else:
                GLib.idle_add(
                    self._flash_status, f"Ready (<b>{name}</b>) — Ctrl+M or click"
                )

        self.transcriber.load_async(name, done)

    # ---------- Recording ----------

    def _on_toggle(self, *_):
        if self.state == "idle":
            self._start_recording()
        elif self.state == "recording":
            self._stop_recording()

    def autostart_recording(self):
        if self.state == "idle":
            self._start_recording()
        return False  # one-shot when scheduled via GLib.idle_add

    def _start_recording(self):
        try:
            self.recorder.start()
        except Exception as exc:
            self._set_status(f"Mic error: {exc}")
            return

        self.state = "recording"
        self.analyzer.reset()
        self._last_voice = time.time()
        self._noise_floor = None
        self.wave.set_mode("recording")
        self.revealer.set_reveal_child(False)
        self._set_status("")
        self._start_anim()

    def _start_anim(self):
        if self._anim_id is None:
            self._anim_id = GLib.timeout_add(config.FPS_MS, self._animate)

    def _animate(self):
        if self.state == "recording":
            samples = self.recorder.tail(config.FFT_SIZE)
            if samples is not None:
                self.wave.set_bars(self.analyzer.update(samples))
                if self._silence_elapsed(samples):
                    self._stop_recording()
            return True
        if self.state == "transcribing":
            self.wave.tick()
            return True
        self._anim_id = None
        return False

    def _silence_elapsed(self, samples):
        """True once no voice has been heard for SILENCE_SECONDS.

        The noise floor drops to any quieter frame instantly and creeps
        up slowly, so it settles at the room's ambient level even if
        recording starts mid-sentence."""
        rms = float(np.sqrt(np.mean(samples**2)))
        if self._noise_floor is None:
            self._noise_floor = max(rms, 1e-4)
        else:
            self._noise_floor = min(self._noise_floor * 1.002, max(rms, 1e-4))
        threshold = max(self._noise_floor * config.VOICE_RATIO, config.VOICE_MIN_RMS)
        if rms > threshold:
            self._last_voice = time.time()
        return time.time() - self._last_voice > config.SILENCE_SECONDS

    def _stop_recording(self):
        self.state = "transcribing"
        self.wave.set_mode("busy")
        audio = self.recorder.stop()

        if len(audio) < config.MIN_SECONDS * config.SAMPLE_RATE:
            self._reset_idle()
            self._flash_status("Too short — try again", 4)
            return

        self._set_status("Transcribing…")
        threading.Thread(
            target=self._transcribe_worker, args=(audio,), daemon=True
        ).start()

    # ---------- Transcription ----------

    def _transcribe_worker(self, audio):
        t0 = time.time()
        try:
            text, peak = self.transcriber.transcribe(audio)
        except Exception as exc:
            GLib.idle_add(self._set_status, f"Error: {exc}")
            GLib.idle_add(self._reset_idle)
            return
        GLib.idle_add(
            self._finish,
            text,
            len(audio) / config.SAMPLE_RATE,
            time.time() - t0,
            peak,
        )

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


def run(autostart: bool = False):
    """Start the app, or just raise the already-running instance.

    With autostart=True (hotkey pressed while Vicy wasn't running),
    recording begins as soon as the window is up — the mic doesn't
    need to wait for the model."""
    try:
        send_command("show")
        print("Vicy is already running — brought to front.")
        return
    except OSError:
        pass

    app = Vicy()
    atexit.register(IpcServer.cleanup)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, Gtk.main_quit)  # SIGINT
    app.present()
    if autostart:
        GLib.idle_add(app.autostart_recording)
    Gtk.main()
