# Vicy

A floating voice-to-text pill for Linux. Hit **Ctrl+M**, speak, pause, and
your words get typed wherever your cursor is. Runs OpenAI Whisper locally,
so nothing ever leaves your machine.

## Install

```bash
git clone https://github.com/ayush00git/vicy.git && cd vicy
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
sudo dnf install -y ydotool && sudo systemctl enable --now ydotool.service
./run.sh --install-hotkey   # bind Ctrl+M
./run.sh
```

## Credits

**Dev**: Claude Fable 5 · **Arch**: Ayush
