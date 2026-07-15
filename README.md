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

## DESCRIPTION

Press **Ctrl+M** anywhere. Speak. Pause for two seconds.
Your words get typed right where your cursor is, straight from a local
OpenAI Whisper model. No cloud, no telemetry.

## INSTALL

    git clone https://github.com/ayush00git/vicy.git && cd vicy
    python3 -m venv --system-site-packages .venv
    .venv/bin/pip install -r requirements.txt
    sudo dnf install -y ydotool && sudo systemctl enable --now ydotool.service
    ./run.sh --install-hotkey    # bind Ctrl+M
    ./run.sh

## CREDITS

    Dev  : Claude Fable 5
    Arch : Ayush
