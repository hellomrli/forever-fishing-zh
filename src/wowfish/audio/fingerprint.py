"""Gain-invariant onset fingerprints and normalized cross-correlation against them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from wowfish.audio.features import SILENCE_LEVEL_DB, MelParams, log_mel

NO_SCORE = -1.0
_BLOCK = 2048
_WORK_BYTES = 8 * 1024 * 1024  # per intermediate array, even for longer fingerprints


@dataclass(frozen=True)
class Fingerprint:
    name: str
    feats: np.ndarray  # (L, n_mels), normalized by normalize_block

    @property
    def length(self) -> int:
        return len(self.feats)


def normalize_block(feats: np.ndarray) -> np.ndarray:
    """Remove each frame's mean across bands (gain, flat background), then zero-mean unit-norm."""
    f = feats - feats.mean(axis=-1, keepdims=True)
    f = f - f.mean(axis=(-2, -1), keepdims=True)
    norm = np.sqrt((f * f).sum(axis=(-2, -1), keepdims=True)) + 1e-9
    return f / norm


def build_fingerprint(
    name: str, samples: np.ndarray, p: MelParams, onset_s: float = 0.8
) -> Fingerprint:
    feats = log_mel(samples[: int(onset_s * p.sample_rate)], p)
    if len(feats) < 2:
        raise ValueError(f"clip {name!r} is too short for a {onset_s}s onset fingerprint")
    return Fingerprint(name, normalize_block(feats).astype(np.float32))


def score_sequence(
    feats: np.ndarray, fp: Fingerprint, *, cancel: Callable[[], bool] | None = None
) -> np.ndarray:
    """NCC of every trailing L-frame window of `feats` with `fp`.

    NO_SCORE before L frames exist and for windows quieter than SILENCE_LEVEL_DB.
    """
    if cancel is not None and cancel():
        raise InterruptedError("音频分析已取消")
    t, length = len(feats), fp.length
    out = np.full(t, NO_SCORE, dtype=np.float32)
    if t < length:
        return out
    windows = sliding_window_view(feats, (length, feats.shape[1]))[:, 0]  # (t-L+1, L, n_mels)
    level = np.convolve(feats.mean(axis=1), np.ones(length) / length, mode="valid")
    window_bytes = length * feats.shape[1] * feats.dtype.itemsize
    block_size = max(1, min(_BLOCK, _WORK_BYTES // window_bytes))
    for start in range(0, len(windows), block_size):
        if cancel is not None and cancel():
            raise InterruptedError("音频分析已取消")
        block = windows[start : start + block_size]
        scores = np.einsum("tij,ij->t", normalize_block(block), fp.feats)
        quiet = level[start : start + len(block)] < SILENCE_LEVEL_DB
        lo = length - 1 + start
        out[lo : lo + len(block)] = np.where(quiet, NO_SCORE, scores)
    return out
