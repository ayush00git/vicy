# Vicy

A tiny floating voice-to-text widget for Linux, powered by OpenAI Whisper
(via [faster-whisper](https://github.com/SYSTRAN/faster-whisper), CPU, fully local).

## Use

```bash
./run.sh
```

- **Press Ctrl+M** from anywhere and just talk. If Vicy isn't running it
  wakes up and starts recording immediately; when you stop speaking for
  ~2 seconds the recording ends and transcribes itself. Clicking the wave
  pill does the same (click again to stop early). While recording, the
  bars dance with your voice's frequency spectrum.
  Silence timing lives in `vicy/config.py` (`SILENCE_SECONDS`, `VOICE_RATIO`).
- The transcript slides out below the pill and is **copied to your clipboard**.
- **Drag** the pill anywhere on screen (a click with motion becomes a drag).
- **Right-click** to switch Whisper models (tiny/base/small/medium), copy the
  last transcript, or quit. `Esc` hides the transcript panel.
- Every capture is saved to `~/.cache/vicy/last.wav` for debugging.
- Default model is `base`; override with `VICY_MODEL=small ./run.sh`.
  Models download once to `~/.cache/huggingface/` on first use.

## Code layout

```
vicy/
  __main__.py    CLI entry (--toggle/--status stay stdlib-light for the hotkey)
  config.py      constants, paths, palette, CSS; forces the X11 backend
  window.py      the pill window: UI, state machine, wiring
  wave_view.py   the frequency-bar widget (Cairo)
  audio.py       Recorder (mic stream) + SpectrumAnalyzer (FFT bands)
  transcriber.py faster-whisper lifecycle + transcription (GTK-free)
  ipc.py         unix-socket client/server for the global hotkey
  hotkey.py      GNOME custom-shortcut registration
  clipboard.py   wl-copy with GTK fallback
```

## Setup (already done)

```bash
python3 -m venv --system-site-packages .venv   # system site: reuses Fedora's GTK bindings
.venv/bin/pip install -r requirements.txt
```

Needs Fedora's `python3-gobject` + GTK3 (preinstalled on Workstation) and
`portaudio` (present). Runs via XWayland (`GDK_BACKEND=x11`) so the
always-on-top hint works under GNOME Wayland.

## Global hotkey

Wayland apps can't grab global keys themselves, so Vicy registers a GNOME
custom shortcut that pokes the running instance over a unix socket
(`$XDG_RUNTIME_DIR/vicy.sock`):

```bash
./run.sh --install-hotkey              # binds Ctrl+M (default)
./run.sh --install-hotkey '<Super>m'   # or any GTK accelerator string
```

CLI verbs: `--toggle` (start/stop recording), `--status`, `--install-hotkey`.
Running `./run.sh` again while Vicy is open just raises the existing window.
