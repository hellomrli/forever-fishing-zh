"""Adaptive motion confirmation in the small region around the player's own bobber."""

from __future__ import annotations

from collections import deque

import cv2
import numpy as np


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
        gray = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
        if self._previous is None:
            self._previous = gray
            return 0.0
        # The bobber occupies only a small part of this crop. The upper 5% captures its
        # splash while the adaptive background rejects steady water animation.
        change = float(np.percentile(np.abs(gray - self._previous), 95))
        self._previous = gray
        background = float(np.median(self._baseline)) if self._baseline else 0.0
        bar = max(self.threshold, background * 2.5 + 3.0)
        self._changes.append((now, change, bar))
        self._baseline.append(change)
        return change

    def confirmed(self, now: float) -> bool:
        return any(now - t <= self.history_s and score >= bar for t, score, bar in self._changes)
