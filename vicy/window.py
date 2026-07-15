"""The floating pill window: UI, state machine, and wiring."""

import atexit
import subprocess
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
from .typer import Typer
from .wave_view import WaveView


def notify(summary, body=""):
    """Desktop notification — the pill itself never shows text."""
    try:
        subprocess.Popen(["notify-send", "-a", "Vicy", summary, body])
    except Exception:
        pass


class Vicy(Gtk.Window):
    def __init__(self):
        super().__init__(title="Vicy")
        self.state = "idle"  # idle | recording | transcribing
        self.model_name = config.DEFAULT_MODEL
        self.last_text = ""
        self._anim_id = None
        self._press_pos = None
        self._win_pos = (0, 0)
        self._dragging = False

        self.recorder = Recorder()
        self.analyzer = SpectrumAnalyzer()
        self.transcriber = Transcriber()
        self.typer = Typer()

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
        # Never take keyboard focus: the user's cursor must stay in the
        # app they're dictating into, or the injected text lands nowhere.
        # DOCK makes the window panel-class, so Mutter also skips it when
        # picking a default window to focus on workspace switches.
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
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

        pill = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pill.set_name("pill")
        pill.set_border_width(6)
        # Margins leave transparent room around the pill for its shadow.
        for side in ("top", "bottom", "start", "end"):
            getattr(pill, f"set_margin_{side}")(12)
        self.add(pill)

        self.wave = WaveView()
        self.wave.set_tooltip_text("Click or Ctrl+M: record · drag to move")
        pill.pack_start(self.wave, True, True, 0)

        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("button-press-event", self._on_press)
        self.connect("button-release-event", self._on_release)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("destroy", Gtk.main_quit)

        # Park near the top-center of the primary monitor.
        display = Gdk.Display.get_default()
        mon = display.get_primary_monitor() or display.get_monitor(0)
        geo = mon.get_geometry()
        self.show_all()
        w, _ = self.get_size()
        self.move(geo.x + (geo.width - w) // 2, geo.y + 48)

    # Click toggles recording; moving past a small threshold becomes a
    # drag. The window is moved manually (not via the window manager)
    # because WMs refuse interactive moves for DOCK windows.

    def _on_press(self, _w, event):
        if event.button == 1:
            self._press_pos = (event.x_root, event.y_root)
            self._win_pos = self.get_position()
            self._dragging = False
            return True
        if event.button == 3:
            self._popup_menu(event)
            return True
        return False

    def _on_motion(self, _w, event):
        if self._press_pos is None:
            return False
        dx = event.x_root - self._press_pos[0]
        dy = event.y_root - self._press_pos[1]
        if self._dragging or abs(dx) > config.DRAG_THRESHOLD or abs(dy) > config.DRAG_THRESHOLD:
            self._dragging = True
            self.move(int(self._win_pos[0] + dx), int(self._win_pos[1] + dy))
        return True

    def _on_release(self, _w, event):
        if event.button == 1 and self._press_pos is not None:
            was_click = not self._dragging
            self._press_pos = None
            self._dragging = False
            if was_click:
                self._on_toggle()
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

    # ---------- Model ----------

    def _load_model_async(self, name):
        def done(error):
            if error:
                GLib.idle_add(notify, "Model failed to load", str(error))

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
            notify("Microphone error", str(exc))
            return

        self.state = "recording"
        self.analyzer.reset()
        self._last_voice = time.time()
        self._noise_floor = None
        self.wave.set_mode("recording")
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
            return

        threading.Thread(
            target=self._transcribe_worker, args=(audio,), daemon=True
        ).start()

    # ---------- Transcription ----------

    def _transcribe_worker(self, audio):
        try:
            text, peak = self.transcriber.transcribe(audio)
        except Exception as exc:
            GLib.idle_add(notify, "Transcription error", str(exc))
            GLib.idle_add(self._reset_idle)
            return
        GLib.idle_add(self._finish, text, peak)

    def _reset_idle(self):
        self.state = "idle"
        self.wave.set_mode("idle")
        return False

    def _finish(self, text, peak=0.0):
        self._reset_idle()
        if text is None:
            notify("Whisper model still loading", "Try again in a moment.")
            return False
        if not text:
            if peak < 0.05:
                notify(
                    "No speech detected",
                    f"Mic peak was {peak * 100:.0f}% — muted or too quiet?",
                )
            return False
        self.last_text = text
        copy_to_clipboard(text)
        # Small delay so the clipboard settles before the paste keystroke.
        GLib.timeout_add(120, self._deliver, text)
        return False

    def _deliver(self, text):
        def fallback():
            notify(
                "Copied — press Ctrl+V to paste",
                "Auto-paste needs the input permission; approve the "
                "dialog if one appeared.",
            )

        self.typer.inject(text, fallback)
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
