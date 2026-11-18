"""Adaptive motion confirmation in the small region around the player's own bobber."""

from __future__ import annotations

from collections import deque

import cv2
import numpy as np

GRID = 64  # the crop is compared at this resolution
BLOB = 8  # side of the neighbourhood whose mean change is scored: 1.6% of the crop
WARMUP = 5  # observations before a change is judged, so the bar reflects this water


class VisualBiteVerifier:
    def __init__(self, threshold: float = 8.0, history_s: float = 1.5) -> None:
        self.threshold = threshold
        self.history_s = history_s
        self._previous: np.ndarray | None = None
        self._baseline: deque[float] = deque(maxlen=30)
        self._changes: deque[tuple[float, float, float]] = deque(maxlen=50)

    def reset(self) -> None:
        self._previous = None
        self._baseline.clear()
        self._changes.clear()

    def observe(self, frame: np.ndarray, now: float) -> float:
        if frame.size == 0:
            return 0.0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (GRID, GRID), interpolation=cv2.INTER_AREA).astype(np.float32)
        if self._previous is None:
            self._previous = gray
            return 0.0
        difference = np.abs(gray - self._previous)
        self._previous = gray
        # The crop is twenty icon areas and the splash is a small part of it, so score the
        # strongest neighbourhood. A percentile of the whole crop only moves once the splash
        # covers that share of it: at the 95th, a splash smaller than the icon never counted.
        edge = BLOB // 2
        inner = slice(edge, GRID - edge + 1)  # positions whose whole neighbourhood is inside
        change = float(cv2.blur(difference, (BLOB, BLOB))[inner, inner].max())
        if len(self._baseline) >= WARMUP:
            # The adaptive background rejects steady water animation and the bobber's drift.
            background = float(np.median(self._baseline))
            self._changes.append((now, change, max(self.threshold, background * 2.5 + 3.0)))
        self._baseline.append(change)
        return change

    def strongest(self, now: float) -> tuple[float, float] | None:
        """The recent change that came closest to its bar, as (change, bar); None before any."""
        recent = [(c, bar) for t, c, bar in self._changes if now - t <= self.history_s]
        return max(recent, key=lambda item: item[0] / item[1], default=None)

    def confirmed(self, now: float) -> bool:
        return any(now - t <= self.history_s and score >= bar for t, score, bar in self._changes)
