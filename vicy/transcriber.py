"""Whisper model lifecycle and transcription (GTK-free)."""

import threading
import time

import numpy as np

from . import config
from .audio import save_last_wav


class Transcriber:
    """Loads faster-whisper models in the background and transcribes."""

    def __init__(self):
        self._model = None
        self._lock = threading.Lock()
        self._wanted = None

    def load_async(self, name: str, on_done):
        """Load `name` on a worker thread; on_done(error_or_None) is
        called from that thread. A load superseded by a newer one is
        dropped silently."""
        with self._lock:
            self._model = None
            self._wanted = name

        def worker():
            try:
                from faster_whisper import WhisperModel

                model = WhisperModel(name, device="cpu", compute_type="int8")
            except Exception as exc:
                on_done(str(exc))
                return
            with self._lock:
                if self._wanted != name:
                    return  # a newer load_async superseded this one
                self._model = model
            on_done(None)

        threading.Thread(target=worker, daemon=True).start()

    def wait(self, timeout: float = 300):
        """Block until the model is ready (or timeout); returns it or None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._model is not None:
                    return self._model
            time.sleep(0.1)
        return None

    def transcribe(self, audio: np.ndarray):
        """Transcribe a 16 kHz mono float32 capture.

        Returns (text, peak). text is None if no model was available.
        Exceptions from inference propagate to the caller.
        """
        model = self.wait()
        if model is None:
            return None, 0.0

        audio = audio - float(audio.mean())  # remove DC offset
        peak = float(np.abs(audio).max()) if len(audio) else 0.0
        save_last_wav(audio)
        if 0 < peak < 0.3:
            audio = audio * (0.9 / peak)  # rescue quiet captures

        segments, _info = model.transcribe(
            audio,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"threshold": 0.35, "min_silence_duration_ms": 500},
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, peak
