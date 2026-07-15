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

import cairo

from . import config
from .audio import Recorder, SpectrumAnalyzer
from .clipboard import copy_to_clipboard
from .ipc import IpcServer, send_command
from .transcriber import Transcriber
from .typer import Typer
from .wave_view import ORB, PILL_SIZE, WaveView


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
        self._hovered = False
        self._collapse_id = None
        self._hover_intent_id = None

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

        # The wave view draws the whole pill (background, shadow, bars)
        # into a fixed-size transparent window, so shape morphs never
        # touch the window manager.
        self.wave = WaveView()
        self.wave.set_tooltip_text("Click or Ctrl+M: record · drag to move")
        self.add(self.wave)

        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.connect("button-press-event", self._on_press)
        self.connect("button-release-event", self._on_release)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("enter-notify-event", self._on_crossing, True)
        self.connect("leave-notify-event", self._on_crossing, False)
        self.connect("destroy", Gtk.main_quit)

        # Load at the top-middle of the primary monitor.
        display = Gdk.Display.get_default()
        mon = display.get_primary_monitor() or display.get_monitor(0)
        geo = mon.get_geometry()
        self.show_all()
        w, _h = self.get_size()
        self.move(geo.x + (geo.width - w) // 2, geo.y + 36)
        self._sync_input_shape(orb_only=True)  # starts collapsed

    def _sync_input_shape(self, orb_only):
        """Restrict the window's input region to the visible shape so
        clicks on the transparent margin pass through to whatever is
        behind the widget (links, buttons, other windows)."""
        gdk_win = self.get_window()
        if gdk_win is None:
            return
        w, h = self.get_size()
        pw, ph = (ORB, ORB) if orb_only else PILL_SIZE
        pw, ph = pw + 4, ph + 4  # small pad so hover pickup isn't fiddly
        rect = cairo.RectangleInt(
            int((w - pw) / 2), int((h - ph) / 2), int(pw), int(ph)
        )
        gdk_win.input_shape_combine_region(cairo.Region(rect), 0, 0)

    # The pill rests as a small orb while idle; hovering it (or any
    # recording/transcribing activity) expands it. After the pointer
    # leaves, it lingers for COLLAPSE_DELAY_MS before collapsing.

    COLLAPSE_DELAY_MS = 2000

    def _in_pill(self, event):
        x0, y0, pw, ph = self.wave.pill_rect()
        return x0 <= event.x <= x0 + pw and y0 <= event.y <= y0 + ph

    # Hover anticipation: react a beat after the pointer commits, so a
    # cursor merely passing through never triggers a morph.
    HOVER_IN_DELAY_MS = 70
    HOVER_OUT_DELAY_MS = 130

    def _set_hovered(self, hovered):
        if hovered == self._hovered:
            return
        self._hovered = hovered
        if self._hover_intent_id is not None:
            GLib.source_remove(self._hover_intent_id)

        def apply():
            self._hover_intent_id = None
            self._refresh_shape()
            return False

        delay = self.HOVER_IN_DELAY_MS if hovered else self.HOVER_OUT_DELAY_MS
        self._hover_intent_id = GLib.timeout_add(delay, apply)

    def _on_crossing(self, _w, event, entered):
        if event.detail == Gdk.NotifyType.INFERIOR:
            return False  # pointer moved between the window and a child
        self._set_hovered(entered and self._in_pill(event))
        return False

    def _refresh_shape(self):
        want_orb = self.state == "idle" and not self._hovered
        if not want_orb:
            if self._collapse_id is not None:
                GLib.source_remove(self._collapse_id)
                self._collapse_id = None
            self._sync_input_shape(orb_only=False)
            self.wave.morph_to(0.0)
            return
        if self._collapse_id is None:

            def collapse():
                self._collapse_id = None
                if self.state == "idle" and not self._hovered:
                    self.wave.morph_to(1.0)
                    self._sync_input_shape(orb_only=True)
                return False

            self._collapse_id = GLib.timeout_add(
                self.COLLAPSE_DELAY_MS, collapse
            )

    # Click toggles recording; moving past a small threshold becomes a
    # drag. The window is moved manually (not via the window manager)
    # because WMs refuse interactive moves for DOCK windows.

    def _on_press(self, _w, event):
        if not self._in_pill(event):
            return False  # ignore clicks on the transparent shadow area
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
            self._set_hovered(self._in_pill(event))
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
        self._refresh_shape()
        self._start_anim()

    def _start_anim(self):
        if self._anim_id is None:
            self._anim_id = GLib.timeout_add(config.FPS_MS, self._animate)

    def _animate(self):
        # Only recording needs this data loop (spectrum + silence watch);
        # the busy pulse and all visual smoothing run inside WaveView.
        if self.state == "recording":
            samples = self.recorder.tail(config.FFT_SIZE)
            if samples is not None:
                self.wave.set_bars(self.analyzer.update(samples))
                if self._silence_elapsed(samples):
                    self._stop_recording()
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
        self._refresh_shape()
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
        # Small delay lets focus settle before keystrokes start landing.
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
