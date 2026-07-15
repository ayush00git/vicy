"""The pill's only face: a row of frequency bars."""

import math

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

    def __init__(self):
        super().__init__()
        self.mode = "idle"  # idle | recording | busy
        self.bars = np.zeros(config.N_BARS)
        self.phase = 0.0
        self.set_size_request(180, 48)
        self.connect("draw", self._draw)

    def set_mode(self, mode):
        self.mode = mode
        if mode != "recording":
            self.bars = np.zeros(config.N_BARS)
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
        step = w / config.N_BARS
        cr.set_line_width(3)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        for i in range(config.N_BARS):
            x = step / 2 + i * step
            if self.mode == "recording":
                v = float(self.bars[i])
                amp = 0.08 + 0.88 * v
                alpha = 0.30 + 0.70 * v  # grey at rest, white on voice
            elif self.mode == "busy":
                amp = 0.25 + 0.20 * math.sin(self.phase - i * 0.55)
                alpha = 0.55
            else:  # idle: taller frozen wave so the pill reads at a glance
                amp = 0.30 + 0.16 * math.sin(i * 0.7)
                alpha = 0.40
            bh = max(1.5, amp * (cy - 4))
            cr.set_source_rgba(*config.BAR_COLOR, alpha)
            cr.move_to(x, cy - bh)
            cr.line_to(x, cy + bh)
            cr.stroke()
