"""The widget face: a self-drawn pill/orb with frequency bars.

The GTK window keeps a fixed size; collapsing and expanding is pure
Cairo drawing, so the morph animates at 60 fps without any
window-manager round trips (which is what made resize-based animation
janky).
"""

import math
import random
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

import cairo
import numpy as np

from . import config

WINDOW_SIZE = (216, 84)  # fixed window, includes room for the shadow
PILL_SIZE = (192, 60)    # expanded capsule
ORB = 48                 # collapsed circle diameter
MORPH_SECONDS = 0.22

BG = (8 / 255, 8 / 255, 10 / 255, 0.97)
BORDER = (1.0, 1.0, 1.0, 0.10)


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
        self._idle_amps = self._random_idle_pattern()
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
        self.queue_draw()

    def set_bars(self, vals):
        self.bars = vals
        self.queue_draw()

    def tick(self):
        self.phase += 0.16
        self.queue_draw()

    # ---------- morph animation ----------

    def morph_to(self, target: float):
        """Animate toward 0.0 (pill) or 1.0 (orb) with ease-out cubic."""
        if target == self._morph_target:
            return
        self._morph_target = target
        self._morph_gen += 1
        gen = self._morph_gen
        start = self.morph
        t0 = time.time()

        def step():
            if gen != self._morph_gen:
                return False
            t = min(1.0, (time.time() - t0) / MORPH_SECONDS)
            ease = 1 - (1 - t) ** 3
            self.morph = start + (target - start) * ease
            self.queue_draw()
            return t < 1.0

        GLib.timeout_add(16, step)  # ~60 fps

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

        # Body and border.
        rounded_rect(cr, x0, y0, pw, ph, radius)
        cr.set_source_rgba(*BG)
        cr.fill_preserve()
        cr.set_source_rgba(*BORDER)
        cr.set_line_width(1)
        cr.stroke_preserve()
        cr.clip()  # bars can never escape the capsule

        cr.set_line_width(3)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        # Crossfade between the full wave and the orb's 5-bar snippet.
        full_alpha = max(0.0, 1.0 - self.morph * 1.8)
        mini_alpha = max(0.0, (self.morph - 0.45) / 0.55)
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
            else:  # idle: random organic wave, regenerated each time
                amp = self._idle_amps[i]
                alpha = 0.40
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
            cr.set_source_rgba(*config.BAR_COLOR, 0.50 * fade)
            cr.move_to(x, cy - bh)
            cr.line_to(x, cy + bh)
            cr.stroke()
