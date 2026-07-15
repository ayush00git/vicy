# vicy(1)

```
        _
 __   _(_) ___ _   _
 \ \ / / |/ __| | | |
  \ V /| | (__| |_| |
   \_/ |_|\___|\__, |
               |___/
```

> voice in, text out. 100% local.

## NAME

**vicy** - a floating voice-to-text pill for Linux

## SYNOPSIS

    vicy [--toggle | --status | --install-hotkey [BINDING]]

## DESCRIPTION

Press **Ctrl+M** anywhere. Speak. Pause for two seconds.
Your words get typed right where your cursor is, straight from a local
OpenAI Whisper model. No cloud, no telemetry, no clipboard noise.

    idle    -> a small glass orb, minding its own business
    hover   -> blooms into a pill of frequency bars
    speak   -> bars dance to your voice's spectrum
    silence -> transcribes and types at your cursor

## INSTALL

    git clone https://github.com/ayush00git/vicy.git && cd vicy
    python3 -m venv --system-site-packages .venv
    .venv/bin/pip install -r requirements.txt
    sudo dnf install -y ydotool && sudo systemctl enable --now ydotool.service
    ./run.sh --install-hotkey    # bind Ctrl+M
    ./run.sh

## OPTIONS

    --toggle             start/stop recording (what the hotkey calls)
    --status             print idle | recording | transcribing
    --install-hotkey     register the GNOME shortcut (default <Control>m)

## FILES

    ~/.cache/vicy/last.wav    the last thing your mic heard
    vicy/config.py            every tunable in one place

## CREDITS

    Dev  : Claude Fable 5
    Arch : Ayush
