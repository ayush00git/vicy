"""The widget face: a self-drawn pill/orb with frequency bars.

The GTK window keeps a fixed size; collapsing and expanding is pure
Cairo drawing, so the morph animates at 60 fps without any
window-manager round trips (which is what made resize-based animation
janky).
"""

import math
import random

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

import cairo
import numpy as np

from . import config

WINDOW_SIZE = (216, 84)  # fixed window, includes room for the shadow
PILL_SIZE = (192, 60)    # expanded capsule
ORB = 48                 # collapsed circle diameter

# Liquid-glass body: translucent smoky base; the glass optics (sheen,
# highlight, specular) are painted as gradient layers on top.
BG = (14 / 255, 14 / 255, 18 / 255, 0.60)
BORDER = (1.0, 1.0, 1.0, 0.16)


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class WaveView(Gtk.DrawingArea):
    """morph 0.0 = full pill, 1.0 = collapsed orb. Modes:
    idle      — organic resting wave (or its 5-bar snippet in the orb)
    recording — each bar tracks one frequency band of the live mic
    busy      — a traveling pulse while Whisper transcribes
    """

    MINI_BARS = 5

    def __init__(self):
        super().__init__()
        self.mode = "idle"  # idle | recording | busy
        self.bars = np.zeros(config.N_BARS)
        self.phase = 0.0
        self.morph = 1.0  # start collapsed
        self._morph_target = 1.0
        self._morph_gen = 0
        self._ambient_on = False
        self.ambient_phase = 0.0
        self._idle_amps = self._random_idle_pattern()
        self._idle_offsets = [
            random.uniform(0, math.tau) for _ in range(config.N_BARS)
        ]
        self._idle_speeds = [
            random.uniform(0.7, 1.4) for _ in range(config.N_BARS)
        ]
        self.set_size_request(*WINDOW_SIZE)
        self.connect("draw", self._draw)

    # ---------- state ----------

    @staticmethod
    def _random_idle_pattern():
        """A fresh organic resting wave — random heights smoothed with
        their neighbors so it looks like a frozen snippet of speech."""
        raw = [random.uniform(0.30, 0.85) for _ in range(config.N_BARS)]
        return [
            (raw[max(0, i - 1)] + raw[i] + raw[min(config.N_BARS - 1, i + 1)]) / 3
            for i in range(config.N_BARS)
        ]

    def set_mode(self, mode):
        self.mode = mode
        if mode != "recording":
            self.bars = np.zeros(config.N_BARS)
        if mode == "idle":
            self._idle_amps = self._random_idle_pattern()
            self._ensure_ambient()
        self.queue_draw()

    def set_bars(self, vals):
        self.bars = vals
        self.queue_draw()

    def tick(self):
        self.phase += 0.16
        self.queue_draw()

    # ---------- morph animation ----------

    @staticmethod
    def _ease_out_cubic(t):
        return 1 - (1 - t) ** 3

    @staticmethod
    def _ease_in_out_cubic(t):
        return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2

    def morph_to(self, target: float):
        """Animate toward 0.0 (pill) or 1.0 (orb).

        Runs on the frame clock (vsync-aligned, one step per rendered
        frame — GLib timeouts drift against the compositor and stutter).
        Expanding uses ease-out, collapsing a calmer ease-in-out; the
        liveliness comes from the ambient wave motion, not overshoot."""
        if target == self._morph_target:
            return
        self._morph_target = target
        self._morph_gen += 1
        gen = self._morph_gen
        start = self.morph
        expanding = target < start
        ease = self._ease_out_cubic if expanding else self._ease_in_out_cubic
        duration = (0.28 if expanding else 0.34) * 1e6  # frame time is µs
        state = {}

        def tick(_widget, clock):
            if gen != self._morph_gen:
                return GLib.SOURCE_REMOVE
            now = clock.get_frame_time()
            t0 = state.setdefault("t0", now)
            t = min(1.0, (now - t0) / duration)
            self.morph = start + (target - start) * ease(t)
            self.queue_draw()
            if t >= 1.0:
                self._ensure_ambient()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        self.add_tick_callback(tick)

    # ---------- ambient idle motion ----------

    def _ambient_active(self):
        """Gentle wave motion runs while the pill is expanded and idle."""
        return self.mode == "idle" and self.morph < 0.6

    def _ensure_ambient(self):
        if self._ambient_on or not self._ambient_active():
            return
        self._ambient_on = True
        state = {}

        def tick(_widget, clock):
            now = clock.get_frame_time()
            last = state.get("last", now)
            state["last"] = now
            if not self._ambient_active():
                self._ambient_on = False
                return GLib.SOURCE_REMOVE
            self.ambient_phase += (now - last) / 1e6 * 2.0  # rad/s
            self.queue_draw()
            return GLib.SOURCE_CONTINUE

        self.add_tick_callback(tick)

    def pill_rect(self):
        """Current visual bounds (x, y, w, h) inside the fixed window."""
        w, h = self.get_allocated_width(), self.get_allocated_height()
        pw = PILL_SIZE[0] + (ORB - PILL_SIZE[0]) * self.morph
        ph = PILL_SIZE[1] + (ORB - PILL_SIZE[1]) * self.morph
        return ((w - pw) / 2, (h - ph) / 2, pw, ph)

    # ---------- drawing ----------

    def _draw(self, _w, cr):
        x0, y0, pw, ph = self.pill_rect()
        radius = ph / 2
        cy = y0 + ph / 2

        # Soft shadow: layered expanding fills.
        for i in range(6, 0, -1):
            rounded_rect(
                cr, x0 - i, y0 - i + 2, pw + 2 * i, ph + 2 * i, radius + i
            )
            cr.set_source_rgba(0, 0, 0, 0.05)
            cr.fill()

        # Body: smoky translucent base.
        rounded_rect(cr, x0, y0, pw, ph, radius)
        cr.set_source_rgba(*BG)
        cr.fill_preserve()

        # Glass sheen: bright at the top, falling to a shaded bottom.
        sheen = cairo.LinearGradient(0, y0, 0, y0 + ph)
        sheen.add_color_stop_rgba(0.00, 1, 1, 1, 0.13)
        sheen.add_color_stop_rgba(0.38, 1, 1, 1, 0.03)
        sheen.add_color_stop_rgba(0.62, 1, 1, 1, 0.00)
        sheen.add_color_stop_rgba(1.00, 0, 0, 0, 0.12)
        cr.set_source(sheen)
        cr.fill_preserve()

        cr.set_source_rgba(*BORDER)
        cr.set_line_width(1)
        cr.stroke_preserve()
        cr.clip()  # everything below stays inside the capsule

        # Inner top highlight: a 1px rim that fades out by mid-height.
        rounded_rect(cr, x0 + 1.5, y0 + 1.5, pw - 3, ph - 3, radius - 1.5)
        rim = cairo.LinearGradient(0, y0, 0, y0 + ph * 0.55)
        rim.add_color_stop_rgba(0, 1, 1, 1, 0.30)
        rim.add_color_stop_rgba(1, 1, 1, 1, 0.00)
        cr.set_source(rim)
        cr.set_line_width(1)
        cr.stroke()

        # Specular blob: a soft off-center glow, the "liquid" touch.
        spec = cairo.RadialGradient(
            x0 + pw * 0.28, y0 + 2, 1, x0 + pw * 0.28, y0 + 2, ph * 0.95
        )
        spec.add_color_stop_rgba(0, 1, 1, 1, 0.10)
        spec.add_color_stop_rgba(1, 1, 1, 1, 0.00)
        cr.set_source(spec)
        cr.paint()

        cr.set_line_width(3)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        # Crossfade between the full wave and the orb's 5-bar snippet
        # (smoothstep, so neither side pops in or out).
        def smoothstep(e0, e1, x):
            t = min(1.0, max(0.0, (x - e0) / (e1 - e0)))
            return t * t * (3 - 2 * t)

        full_alpha = 1.0 - smoothstep(0.15, 0.55, self.morph)
        mini_alpha = smoothstep(0.50, 0.90, self.morph)
        if full_alpha > 0:
            self._draw_full(cr, x0, cy, pw, ph, full_alpha)
        if mini_alpha > 0:
            self._draw_mini(cr, x0, cy, pw, ph, mini_alpha)

    def _draw_full(self, cr, x0, cy, pw, ph, fade):
        step = (pw - 12) / config.N_BARS
        for i in range(config.N_BARS):
            x = x0 + 6 + step / 2 + i * step
            if self.mode == "recording":
                v = float(self.bars[i])
                amp = 0.08 + 0.88 * v
                alpha = 0.30 + 0.70 * v  # grey at rest, white on voice
            elif self.mode == "busy":
                amp = 0.25 + 0.20 * math.sin(self.phase - i * 0.55)
                alpha = 0.55
            else:  # idle: organic wave, each bar breathing at its own pace
                wob = 0.82 + 0.18 * math.sin(
                    self.ambient_phase * self._idle_speeds[i]
                    + self._idle_offsets[i]
                )
                amp = self._idle_amps[i] * wob
                alpha = 0.48
            # Taper the outermost bars into the rounded ends.
            edge = min(i, config.N_BARS - 1 - i)
            amp *= min(1.0, (edge + 1) / 4)
            bh = max(1.5, amp * (ph / 2 - 6))
            cr.set_source_rgba(*config.BAR_COLOR, alpha * fade)
            cr.move_to(x, cy - bh)
            cr.line_to(x, cy + bh)
            cr.stroke()

    def _draw_mini(self, cr, x0, cy, pw, ph, fade):
        envelope = (0.45, 0.80, 1.0, 0.80, 0.45)
        step = (pw - 8) / self.MINI_BARS
        pick = max(1, config.N_BARS // self.MINI_BARS)
        for i in range(self.MINI_BARS):
            amp = self._idle_amps[min(i * pick, config.N_BARS - 1)]
            amp *= envelope[i]
            bh = max(1.5, amp * (ph / 2 - 7))
            x = x0 + 4 + step / 2 + i * step
            cr.set_source_rgba(*config.BAR_COLOR, 0.58 * fade)
            cr.move_to(x, cy - bh)
            cr.line_to(x, cy + bh)
            cr.stroke()
