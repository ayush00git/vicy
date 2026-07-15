"""The widget face: a self-drawn pill/orb with frequency bars.

Animation architecture: one frame-clock loop (add_tick_callback)
advances a handful of state values — a spring-driven morph, a lagged
opacity crossfade, a shared master phase, and per-bar smoothed
amplitudes — and _draw() re-derives the whole picture from them each
frame. The window is never resized or moved; everything is repaint.
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

# Springs (omega = stiffness rad/s, zeta = damping ratio). The morph is
# lightly underdamped for a tiny natural overshoot; the opacity
# crossfade is critically damped and softer, so it lags the geometry.
MORPH_OMEGA, MORPH_ZETA = 26.0, 0.88
FADE_OMEGA, FADE_ZETA = 18.0, 1.0

MASTER_SPEED = 2.0   # rad/s — the one shared timing source
ATTACK_TAU = 0.035   # bars rise fast…
DECAY_TAU = 0.16     # …and fall slower
BREATHE_TAU = 0.25   # ambient weight fade-in/out


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def smoothstep(e0, e1, x):
    t = min(1.0, max(0.0, (x - e0) / (e1 - e0)))
    return t * t * (3 - 2 * t)


class WaveView(Gtk.DrawingArea):
    """morph 0.0 = full pill, 1.0 = collapsed orb. Modes:
    idle      — organic resting wave (breathing gently while expanded)
    recording — each bar tracks one frequency band of the live mic
    busy      — a traveling pulse while Whisper transcribes
    """

    MINI_BARS = 5

    def __init__(self):
        super().__init__()
        n = config.N_BARS
        self.mode = "idle"  # idle | recording | busy

        # Spring state: geometry (morph) and lagged opacity (fade).
        self.morph = 1.0  # start collapsed
        self._morph_target = 1.0
        self._morph_vel = 0.0
        self._fade = 1.0
        self._fade_target = 1.0
        self._fade_vel = 0.0

        # Shared timing source for ambient wobble, breathing, glow.
        self.master_phase = 0.0
        self._breathe_w = 0.0  # ambient weight, smoothed 0..1

        # Bars: raw targets fed by the app, displayed values smoothed
        # toward them (fast attack, slow decay) every frame.
        self._bar_targets = np.zeros(n)
        self._amp_t = np.zeros(n)
        self._alpha_t = np.zeros(n)
        self._amp_disp = np.zeros(n)
        self._alpha_disp = np.full(n, 0.48)

        self._idx = np.arange(n)
        edge = np.minimum(self._idx, n - 1 - self._idx)
        # Taper the outermost bars into the rounded ends.
        self._taper = np.minimum(1.0, (edge + 1) / 4.0)

        self._new_idle_pattern()
        self._amp_disp[:] = self._idle_amps * self._taper

        self._anim_on = False
        self._last_us = None

        self.set_size_request(*WINDOW_SIZE)
        self.connect("draw", self._draw)

    # ---------- patterns ----------

    def _new_idle_pattern(self):
        """A fresh organic resting wave — random heights smoothed with
        their neighbors, plus a random phase and speed per bar so the
        ambient motion never moves in lockstep."""
        n = config.N_BARS
        raw = [random.uniform(0.30, 0.85) for _ in range(n)]
        self._idle_amps = np.array(
            [
                (raw[max(0, i - 1)] + raw[i] + raw[min(n - 1, i + 1)]) / 3
                for i in range(n)
            ]
        )
        self._idle_offsets = np.array(
            [random.uniform(0, math.tau) for _ in range(n)]
        )
        self._idle_speeds = np.array(
            [random.uniform(0.7, 1.4) for _ in range(n)]
        )

    # ---------- public state ----------

    def set_mode(self, mode):
        self.mode = mode
        if mode != "recording":
            self._bar_targets[:] = 0.0
        if mode == "idle":
            self._new_idle_pattern()
        self._ensure_anim()
        self.queue_draw()

    def set_bars(self, vals):
        np.copyto(self._bar_targets, vals)
        self._ensure_anim()

    def morph_to(self, target: float):
        """Retarget the springs toward 0.0 (pill) or 1.0 (orb)."""
        if target == self._morph_target:
            return
        self._morph_target = float(target)
        self._fade_target = float(target)
        self._ensure_anim()

    # ---------- geometry ----------

    def pill_rect(self):
        """Current visual bounds (x, y, w, h) inside the fixed window.
        While idle and expanded the capsule itself breathes subtly."""
        w, h = self.get_allocated_width(), self.get_allocated_height()
        pw = PILL_SIZE[0] + (ORB - PILL_SIZE[0]) * self.morph
        ph = PILL_SIZE[1] + (ORB - PILL_SIZE[1]) * self.morph
        b = self._breathe_w
        if b > 1e-3:
            pw += 2.0 * b * math.sin(self.master_phase * 0.9)
            ph += 1.0 * b * math.sin(self.master_phase * 0.9 + 0.9)
        return ((w - pw) / 2, (h - ph) / 2, pw, ph)

    # ---------- master animation loop ----------

    def _ambient_active(self):
        return self.mode == "idle" and self.morph < 0.6

    def _ensure_anim(self):
        if self._anim_on:
            return
        self._anim_on = True
        self._last_us = None
        self.add_tick_callback(self._tick)

    @staticmethod
    def _spring(x, v, target, omega, zeta, dt):
        a = -omega * omega * (x - target) - 2.0 * zeta * omega * v
        v += a * dt
        x += v * dt
        return x, v

    def _tick(self, _widget, clock):
        now = clock.get_frame_time()
        last = self._last_us if self._last_us is not None else now
        self._last_us = now
        dt = min(0.05, max(1e-4, (now - last) / 1e6))

        self.master_phase += dt * MASTER_SPEED

        # Springs: geometry, then the softer opacity crossfade.
        self.morph, self._morph_vel = self._spring(
            self.morph, self._morph_vel, self._morph_target,
            MORPH_OMEGA, MORPH_ZETA, dt,
        )
        if (
            abs(self.morph - self._morph_target) < 1e-3
            and abs(self._morph_vel) < 0.02
        ):
            self.morph, self._morph_vel = self._morph_target, 0.0
        self._fade, self._fade_vel = self._spring(
            self._fade, self._fade_vel, self._fade_target,
            FADE_OMEGA, FADE_ZETA, dt,
        )
        if (
            abs(self._fade - self._fade_target) < 1e-3
            and abs(self._fade_vel) < 0.02
        ):
            self._fade, self._fade_vel = self._fade_target, 0.0

        # Ambient weight eases in/out so breathing never pops on/off.
        bt = 1.0 if self._ambient_active() else 0.0
        self._breathe_w += (bt - self._breathe_w) * (
            1 - math.exp(-dt / BREATHE_TAU)
        )
        if abs(self._breathe_w - bt) < 1e-3:
            self._breathe_w = bt

        # Bars: displayed values chase mode targets (fast up, slow down).
        self._compute_targets()
        ka = 1 - math.exp(-dt / ATTACK_TAU)
        kd = 1 - math.exp(-dt / DECAY_TAU)
        diff = self._amp_t - self._amp_disp
        self._amp_disp += diff * np.where(diff > 0.0, ka, kd)
        adiff = self._alpha_t - self._alpha_disp
        self._alpha_disp += adiff * np.where(adiff > 0.0, ka, kd)

        self.queue_draw()

        if self._settled():
            self._anim_on = False
            self._last_us = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _compute_targets(self):
        """Per-bar amplitude/alpha targets for the current mode. Mode
        switches glide because the displayed values chase these."""
        if self.mode == "recording":
            v = self._bar_targets
            np.multiply(v, 0.88, out=self._amp_t)
            self._amp_t += 0.08
            np.multiply(v, 0.70, out=self._alpha_t)
            self._alpha_t += 0.30
        elif self.mode == "busy":
            np.sin(self.master_phase * 2.4 - self._idx * 0.55, out=self._amp_t)
            self._amp_t *= 0.20
            self._amp_t += 0.25
            self._alpha_t[:] = 0.55
        else:  # idle
            b = self._breathe_w
            if b > 1e-3:
                # wob == 1 at b=0; full ±18% organic sway at b=1.
                wob = 1.0 - 0.18 * b * (
                    1.0
                    - np.sin(
                        self.master_phase * self._idle_speeds
                        + self._idle_offsets
                    )
                )
                np.multiply(self._idle_amps, wob, out=self._amp_t)
            else:
                self._amp_t[:] = self._idle_amps
            self._alpha_t[:] = 0.48
        self._amp_t *= self._taper

    def _settled(self):
        """The loop stops only when truly still: idle, collapsed, both
        springs at rest, breathing faded out, bars done decaying."""
        if self.mode != "idle" or self._ambient_active():
            return False
        if self.morph != self._morph_target or self._fade != self._fade_target:
            return False
        if self._breathe_w > 1e-3:
            return False
        return float(np.max(np.abs(self._amp_t - self._amp_disp))) < 0.004

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

        # Specular blob: a soft off-center glow that pulses gently with
        # the same master phase as the breathing.
        pulse = 1.0 + 0.22 * self._breathe_w * math.sin(
            self.master_phase * 0.9 + 1.3
        )
        spec = cairo.RadialGradient(
            x0 + pw * 0.28, y0 + 2, 1,
            x0 + pw * 0.28, y0 + 2, ph * 0.95 * pulse,
        )
        spec.add_color_stop_rgba(0, 1, 1, 1, 0.10 * pulse)
        spec.add_color_stop_rgba(1, 1, 1, 1, 0.00)
        cr.set_source(spec)
        cr.paint()

        cr.set_line_width(3)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        # Crossfade runs on the lagged fade spring, not the geometry, so
        # shape leads and opacity follows for a richer transition.
        full_alpha = 1.0 - smoothstep(0.15, 0.55, self._fade)
        mini_alpha = smoothstep(0.50, 0.90, self._fade)
        if full_alpha > 0:
            self._draw_full(cr, x0, cy, pw, ph, full_alpha)
        if mini_alpha > 0:
            self._draw_mini(cr, x0, cy, pw, ph, mini_alpha)

    def _draw_full(self, cr, x0, cy, pw, ph, fade):
        step = (pw - 12) / config.N_BARS
        half = ph / 2 - 6
        amps = self._amp_disp
        alphas = self._alpha_disp
        for i in range(config.N_BARS):
            bh = max(1.5, amps[i] * half)
            x = x0 + 6 + step / 2 + i * step
            cr.set_source_rgba(*config.BAR_COLOR, alphas[i] * fade)
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
