"""A reader thread in front of a blocking AudioSource so the frame loop never waits on audio.

Real devices block in `read(n)` until n frames exist, and soundcard's WASAPI capture buffer is
only one block long, so a loop that also grabs frames would fall behind or lose audio. Here the
thread reads block after block into a deque; `read(n)` hands back whatever is there (up to n)
without blocking, possibly nothing.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

JOIN_S = 2.0


class ThreadedSource:
    def __init__(
        self,
        inner: Any,
        *,
        block: int = 1024,
        max_s: float = 4.0,
        factory: Callable[[], Any] | None = None,
    ) -> None:
        self.inner = inner
        self.sample_rate = int(inner.sample_rate)
        self.name = getattr(inner, "name", type(inner).__name__)
        self.block = block
        self._max_s = max_s
        self._factory = factory
        self._max_frames = int(max_s * self.sample_rate)
        self._chunks: deque[np.ndarray] = deque()
        self._frames = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="wowfish-audio", daemon=True)

    def __enter__(self) -> ThreadedSource:
        self._thread.start()
        deadline = time.monotonic() + 8.0
        while not self._ready.wait(0.05):
            if self._stop.is_set():
                raise RuntimeError("音频设备启动已取消")
            if time.monotonic() >= deadline:
                self.cancel()
                raise RuntimeError("音频设备启动超时")
        if self._stop.is_set():
            raise RuntimeError("音频设备启动已取消")
        if self.error is not None:
            raise RuntimeError(f"音频设备打开失败：{self.error}") from self.error
        self.name = getattr(self.inner, "name", self.name)
        return self

    def __exit__(self, *exc: object) -> None:
        self.cancel()
        self._thread.join(JOIN_S)

    @property
    def fallback_reason(self) -> str:
        return str(getattr(self.inner, "fallback_reason", ""))

    def cancel(self) -> None:
        self._stop.set()
        cancel = getattr(self.inner, "cancel", None)
        if cancel is not None:
            cancel()

    def restart(self) -> None:
        self.cancel()
        self._thread.join(JOIN_S)
        if self._thread.is_alive():
            raise RuntimeError("旧音频设备仍在关闭，请停止后重新开始")
        inner = self._factory() if self._factory is not None else self.inner
        fresh = ThreadedSource(inner, block=self.block, max_s=self._max_s, factory=self._factory)
        self.__dict__.update(fresh.__dict__)
        # The thread's bound target must belong to this instance, not the temporary instance.
        self._thread = threading.Thread(target=self._run, name="foreverfish-audio", daemon=True)
        self.__enter__()

    def _run(self) -> None:
        try:
            if self._stop.is_set():
                return
            # Open/read/close all run on this thread: COM interfaces keep their owning thread.
            with self.inner:
                self.name = getattr(self.inner, "name", self.name)
                self.sample_rate = int(self.inner.sample_rate)
                self._max_frames = max(1, int(self._max_s * self.sample_rate))
                self._ready.set()
                while not self._stop.is_set():
                    samples = np.asarray(self.inner.read(self.block), dtype=np.float32)
                    if not len(samples):
                        if getattr(self.inner, "exhausted", False):
                            break
                        self._stop.wait(0.01)
                        continue
                    with self._lock:
                        self._chunks.append(samples)
                        self._frames += len(samples)
                        while self._frames > self._max_frames:
                            oldest = self._chunks.popleft()
                            drop = min(len(oldest), self._frames - self._max_frames)
                            self._frames -= drop
                            if drop < len(oldest):
                                self._chunks.appendleft(oldest[drop:])
                    if getattr(self.inner, "exhausted", False):
                        break
        except BaseException as e:  # surface on the next read instead of dying silently
            if not self._stop.is_set():
                self.error = e
        finally:
            self._ready.set()

    def clear(self) -> int:
        """Drop everything buffered so far; return how many frames were dropped."""
        with self._lock:
            dropped = self._frames
            self._chunks.clear()
            self._frames = 0
        return dropped

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return (
                self._ready.is_set()
                and not self._thread.is_alive()
                and self._frames == 0
                and self.error is None
            )

    def read(self, n_frames: int) -> np.ndarray:
        """Up to n_frames of buffered audio, immediately; empty when nothing has arrived."""
        if self.error is not None:
            raise RuntimeError(f"audio capture stopped: {self.error}") from self.error
        out: list[np.ndarray] = []
        got = 0
        with self._lock:
            while self._chunks and got < n_frames:
                chunk = self._chunks.popleft()
                take = min(len(chunk), n_frames - got)
                out.append(chunk[:take])
                got += take
                self._frames -= take
                if take < len(chunk):
                    self._chunks.appendleft(chunk[take:])
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out)
