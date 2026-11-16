"""AudioSource over a `soundcard` loopback microphone (WASAPI loopback, Pulse/PipeWire monitor)."""

from __future__ import annotations

import sys
import threading
from contextlib import suppress
from typing import Any

import numpy as np

from wowfish.audio._com import ComApartment


class SoundcardSource:
    def __init__(self, mic: Any, sample_rate: int = 22050, blocksize: int = 1024) -> None:
        self.mic = mic
        self.name = str(mic.name)
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self._rec: Any = None
        self._apartment = ComApartment()
        self._cancel = threading.Event()

    def __enter__(self) -> SoundcardSource:
        # CoreAudio only takes a buffer inside the device's range (BlackHole: 15-512 frames) and
        # soundcard raises rather than clamp. The buffer only sets latency; reads are any length.
        blocksize = None if sys.platform == "darwin" else self.blocksize
        self._cancel.clear()
        self._apartment.__enter__()
        try:
            # WASAPI's mono capture in soundcard can produce invalid samples on stereo
            # endpoints. Capture the endpoint's channels and mix them down ourselves.
            channels = None if sys.platform == "win32" else 1
            self._rec = self.mic.recorder(
                samplerate=self.sample_rate, channels=channels, blocksize=blocksize
            )
            self._rec.__enter__()
        except BaseException:
            if self._rec is not None:
                with suppress(Exception):
                    self._rec.__exit__(*sys.exc_info())
                self._rec = None
            self._apartment.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self._rec is not None:
                self._rec.__exit__(*exc)
        finally:
            self._rec = None
            self._apartment.__exit__(*exc)

    def cancel(self) -> None:
        self._cancel.set()

    def read(self, n_frames: int) -> np.ndarray:
        if self._cancel.is_set():
            return np.zeros(0, np.float32)
        data = np.asarray(self._rec.record(numframes=n_frames), dtype=np.float32)
        if data.ndim == 2:
            data = data[:, 0] if data.shape[1] == 1 else data.mean(axis=1)
        return np.ascontiguousarray(data, dtype=np.float32)
