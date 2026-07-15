"""The pill's only face: a row of frequency bars."""

import math
import random

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

import cairo
import numpy as np

from . import config


class WaveView(Gtk.DrawingArea):
    """idle      — calm resting wave, always visible
    recording — each bar tracks one frequency band of the live mic
    busy      — a traveling pulse while Whisper transcribes
    """

    FULL_SIZE = (180, 48)
    MINI_SIZE = (36, 36)
    MINI_BARS = 5

    def __init__(self):
        super().__init__()
        self.mode = "idle"  # idle | recording | busy
        self.mini = False   # collapsed idle orb
        self.bars = np.zeros(config.N_BARS)
        self.phase = 0.0
        self._idle_amps = self._random_idle_pattern()
        self.set_size_request(*self.FULL_SIZE)
        self.connect("draw", self._draw)

    @staticmethod
    def _random_idle_pattern():
        """A fresh organic resting wave — random heights smoothed with
        their neighbors so it looks like a frozen snippet of speech."""
        raw = [random.uniform(0.30, 0.85) for _ in range(config.N_BARS)]
        return [
            (raw[max(0, i - 1)] + raw[i] + raw[min(config.N_BARS - 1, i + 1)]) / 3
            for i in range(config.N_BARS)
        ]

    def set_mini(self, mini):
        if self.mini != mini:
            self.mini = mini
            self.set_size_request(*(self.MINI_SIZE if mini else self.FULL_SIZE))
            self.queue_draw()

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

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        cy = h / 2
        cr.set_line_width(3)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        if self.mini:
            # Collapsed idle orb: a few bars sampled from the idle wave.
            step = w / self.MINI_BARS
            pick = max(1, config.N_BARS // self.MINI_BARS)
            for i in range(self.MINI_BARS):
                amp = self._idle_amps[min(i * pick, config.N_BARS - 1)]
                bh = max(1.5, amp * (cy - 4))
                x = step / 2 + i * step
                cr.set_source_rgba(*config.BAR_COLOR, 0.40)
                cr.move_to(x, cy - bh)
                cr.line_to(x, cy + bh)
                cr.stroke()
            return

        step = w / config.N_BARS
        for i in range(config.N_BARS):
            x = step / 2 + i * step
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
            # Taper the outermost bars so they stay inside the pill's
            # rounded ends instead of poking past the curve.
            edge = min(i, config.N_BARS - 1 - i)
            amp *= min(1.0, (edge + 1) / 4)
            bh = max(1.5, amp * (cy - 4))
            cr.set_source_rgba(*config.BAR_COLOR, alpha)
            cr.move_to(x, cy - bh)
            cr.line_to(x, cy + bh)
            cr.stroke()
