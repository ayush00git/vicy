# Vicy

A tiny floating voice-to-text widget for Linux, powered by OpenAI Whisper
(via [faster-whisper](https://github.com/SYSTRAN/faster-whisper), CPU, fully local).

## Use

```bash
./run.sh
```

- **Click the mic** 🎤 to record, click again ⏹ to stop.
- The transcript slides out below the pill and is **copied to your clipboard**.
- **Drag** the pill anywhere on screen.
- **Right-click** to switch Whisper models (tiny/base/small/medium), copy the
  last transcript, or quit. `Esc` hides the transcript panel.
- Default model is `base`; override with `VICY_MODEL=small ./run.sh`.
  Models download once to `~/.cache/huggingface/` on first use.

## Setup (already done)

```bash
python3 -m venv --system-site-packages .venv   # system site: reuses Fedora's GTK bindings
.venv/bin/pip install -r requirements.txt
```

Needs Fedora's `python3-gobject` + GTK3 (preinstalled on Workstation) and
`portaudio` (present). Runs via XWayland (`GDK_BACKEND=x11`) so the
always-on-top hint works under GNOME Wayland.
