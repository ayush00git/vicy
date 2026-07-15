"""Microphone capture and live spectrum analysis."""

import os

import numpy as np
import sounddevice as sd

from . import config


class Recorder:
    """Streams the default mic into memory at 16 kHz mono float32."""

    def __init__(self):
        self._stream = None
        self._chunks = []

    def start(self):
        """Open the input stream. Raises on mic errors."""
        self._chunks = []

        def audio_cb(indata, _frames, _time, _status):
            self._chunks.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=config.SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=audio_cb,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        """Close the stream and return everything captured as a 1-D array."""
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
        self._stream = None

        if self._chunks:
            audio = np.concatenate(self._chunks, axis=0).reshape(-1)
        else:
            audio = np.zeros(0, dtype="float32")
        self._chunks = []
        return audio

    def tail(self, n: int):
        """Return the most recent `n` samples (zero-padded), or None if
        nothing has been captured yet."""
        parts, have = [], 0
        for chunk in reversed(self._chunks):
            parts.append(chunk[:, 0])
            have += len(chunk)
            if have >= n:
                break
        if not parts:
            return None
        x = np.concatenate(parts[::-1])[-n:]
        if len(x) < n:
            x = np.pad(x, (n - len(x), 0))
        return x


class SpectrumAnalyzer:
    """Turns raw samples into smoothed, normalized frequency-band bars."""

    def __init__(self, n_bars: int = config.N_BARS):
        freqs = np.fft.rfftfreq(config.FFT_SIZE, 1 / config.SAMPLE_RATE)
        edges = np.geomspace(config.BAND_LO, config.BAND_HI, n_bars + 1)
        self._band_masks = [
            (freqs >= lo) & (freqs < hi) for lo, hi in zip(edges[:-1], edges[1:])
        ]
        self._window = np.hanning(config.FFT_SIZE)
        self._n_bars = n_bars
        self.reset()

    def reset(self):
        self.bars = np.zeros(self._n_bars)
        self._peak = 3.0

    def update(self, samples: np.ndarray) -> np.ndarray:
        """Feed the latest FFT_SIZE samples; returns the new bar heights.

        Adaptive normalization with a floor keeps silence calm, and each
        bar gets fast-attack / slow-decay smoothing.
        """
        mag = np.abs(np.fft.rfft(samples * self._window))
        bands = np.array(
            [float(mag[m].mean()) if m.any() else 0.0 for m in self._band_masks]
        )
        self._peak = max(self._peak * 0.99, float(bands.max()), 3.0)
        vals = np.clip(bands / self._peak, 0.0, 1.0) ** 0.6
        self.bars = np.maximum(vals, self.bars * 0.72)
        return self.bars


def save_last_wav(audio: np.ndarray):
    """Keep the last capture at ~/.cache/vicy/last.wav for debugging."""
    try:
        import wave

        os.makedirs(config.CACHE_DIR, exist_ok=True)
        with wave.open(os.path.join(config.CACHE_DIR, "last.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(config.SAMPLE_RATE)
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            w.writeframes(pcm.tobytes())
    except Exception:
        pass
