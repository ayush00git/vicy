# Vicy

A floating voice-to-text pill for Linux. Press **Ctrl+M**, speak, pause —
your words get typed right where your cursor is. Powered by OpenAI Whisper,
100% local, nothing leaves your machine.

**Architect:** Ayush · **Developer:** Claude Code

## Install

```bash
git clone https://github.com/ayush00git/vicy.git && cd vicy
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt

# auto-typing daemon (Fedora)
sudo dnf install -y ydotool
sudo mkdir -p /etc/systemd/system/ydotool.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/bin/ydotoold --socket-path=/tmp/.ydotool_socket --socket-own=%s:%s\n' "$(id -u)" "$(id -g)" | sudo tee /etc/systemd/system/ydotool.service.d/socket.conf
sudo systemctl daemon-reload && sudo systemctl enable --now ydotool.service

./run.sh --install-hotkey   # binds Ctrl+M globally (GNOME)
./run.sh                    # start the pill
```

## Use

- **Ctrl+M** (or click the pill) → speak → stop talking for ~2 s → the text
  appears at your cursor. It's also on your clipboard.
- **Drag** the pill anywhere · **right-click** for Whisper models and quit.

Tunables (silence timing, model, colors) live in `vicy/config.py`.
