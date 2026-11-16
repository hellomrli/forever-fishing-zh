"""An AudioSource that saves everything read through it to a WAV when it closes."""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import numpy as np


class RecordingSource:
    def __init__(self, inner: Any, path: Path) -> None:
        self.inner = inner
        self.path = Path(path)
        self.sample_rate = int(inner.sample_rate)
        self.name = getattr(inner, "name", type(inner).__name__)
        self._writer: Any = None

    def __enter__(self) -> RecordingSource:
        self.inner.__enter__()
        try:
            self.name = getattr(self.inner, "name", self.name)
            self.sample_rate = int(self.inner.sample_rate)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._writer = wave.open(str(self.path), "wb")
            self._writer.setnchannels(1)
            self._writer.setsampwidth(2)
            self._writer.setframerate(self.sample_rate)
        except BaseException:
            self.inner.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.inner.__exit__(*exc)
        finally:
            if self._writer is not None:
                self._writer.close()
                self._writer = None

    def cancel(self) -> None:
        cancel = getattr(self.inner, "cancel", None)
        if cancel is not None:
            cancel()

    def restart(self) -> None:
        self.inner.restart()
        self.name = getattr(self.inner, "name", self.name)

    @property
    def fallback_reason(self) -> str:
        return str(getattr(self.inner, "fallback_reason", ""))

    def clear(self) -> int:
        """Drop the inner source's backlog; the recording keeps only what the engine heard."""
        clear = getattr(self.inner, "clear", None)
        return clear() if clear is not None else 0

    def read(self, n_frames: int) -> np.ndarray:
        samples = self.inner.read(n_frames)
        if len(samples) and self._writer is not None:
            pcm = np.clip(np.asarray(samples, np.float32) * 32767, -32768, 32767).astype("<i2")
            self._writer.writeframesraw(pcm.tobytes())
        return samples
