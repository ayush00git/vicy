"""Shared constants, paths, and environment setup.

Import this module before anything that touches GTK: it forces the X11
backend, because GNOME on Wayland doesn't let native Wayland windows set
keep-above but honors it for XWayland windows.
"""

import os

os.environ.setdefault("GDK_BACKEND", "x11")

# Paths
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOCK_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "vicy.sock")
CACHE_DIR = os.path.expanduser("~/.cache/vicy")

# Hotkey / models
HOTKEY_DEFAULT = "<Control>m"
MODELS = ("tiny", "base", "small", "medium")
DEFAULT_MODEL = os.environ.get("VICY_MODEL", "base")

# Audio
SAMPLE_RATE = 16000
MIN_SECONDS = 0.3    # discard captures shorter than this
FFT_SIZE = 1024      # ~64 ms of audio per spectrum frame
BAND_LO, BAND_HI = 80.0, 4000.0  # voice range mapped across the bars

# Silence auto-stop: recording ends after this long without voice.
# Voice = frame RMS above max(noise_floor * VOICE_RATIO, VOICE_MIN_RMS),
# where the noise floor adapts (drops fast in quiet, creeps up slowly).
SILENCE_SECONDS = 2.0
VOICE_RATIO = 2.0
VOICE_MIN_RMS = 0.02

# UI
N_BARS = 36
FPS_MS = 33          # ~30 fps animation while active
DRAG_THRESHOLD = 6   # px of motion before a click becomes a window drag

# Monochrome dark palette: the pill is black, bars are white with the
# grey shades coming from alpha over the black background.
BAR_COLOR = (1.0, 1.0, 1.0)

CSS = b"""
#vicywin { background: transparent; }
#pill {
    background: rgba(8, 8, 10, 0.97);
    border-radius: 24px;
    border: 1px solid rgba(255, 255, 255, 0.10);
    box-shadow: 0 2px 14px rgba(0, 0, 0, 0.35);
}
"""
