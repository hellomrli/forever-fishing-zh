"""Numerical accuracy, bounded work buffers and cancellation of offline audio analysis."""

from __future__ import annotations

import numpy as np
import pytest
from numpy.lib.stride_tricks import sliding_window_view

from wowfish.audio.features import DEFAULT_MEL, log_mel, mel_filterbank
from wowfish.audio.fingerprint import Fingerprint, normalize_block, score_sequence
from wowfish.audio.verify import bite_report, score_recording


def test_log_mel_batches_preserve_the_spectrogram_and_limit_fft_size(monkeypatch):
    p = DEFAULT_MEL
    samples = np.random.default_rng(10).normal(0, 0.1, p.hop * 3500 + p.n_fft).astype(np.float32)
    frames = sliding_window_view(samples, p.n_fft)[:: p.hop]
    window = np.hanning(p.n_fft).astype(np.float32)
    power = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
    expected = (10 * np.log10(power @ mel_filterbank(p).T + 1e-10)).astype(np.float32)
    transform = np.fft.rfft
    sizes = []

    def measured_fft(block, *args, **kwargs):
        sizes.append(len(block))
        return transform(block, *args, **kwargs)

    monkeypatch.setattr(np.fft, "rfft", measured_fft)
    actual = log_mel(samples, p)
    np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-5)
    assert len(sizes) > 1 and max(sizes) <= 1024


def test_fingerprint_scoring_limits_work_buffers_without_changing_scores(monkeypatch):
    import wowfish.audio.fingerprint as module

    rng = np.random.default_rng(42)
    template = rng.normal(-8, 3, (270, DEFAULT_MEL.n_mels)).astype(np.float32)
    fp = Fingerprint("water", normalize_block(template))
    feats = rng.normal(-8, 3, (1000, DEFAULT_MEL.n_mels)).astype(np.float32)
    feats[300:570] = template
    original = module.normalize_block
    sizes = []

    def measured_normalization(block):
        sizes.append(block.nbytes)
        return original(block)

    monkeypatch.setattr(module, "normalize_block", measured_normalization)
    scores = score_sequence(feats, fp)
    assert max(sizes) <= 8 * 1024 * 1024
    assert int(np.argmax(scores)) == 569 and scores[569] == pytest.approx(1, abs=1e-6)
    for index in (269, 400, 999):
        expected = float(np.sum(original(feats[index - fp.length + 1 : index + 1]) * fp.feats))
        assert scores[index] == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("stage", ["features", "fingerprint", "recording"])
def test_audio_analysis_can_cancel_between_batches(stage):
    p = DEFAULT_MEL
    fp = Fingerprint("water", np.zeros((8, p.n_mels), np.float32))
    checks = 0

    def cancel():
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(InterruptedError, match="取消"):
        if stage == "features":
            log_mel(np.zeros(p.hop * 3500 + p.n_fft, np.float32), p, cancel=cancel)
        elif stage == "fingerprint":
            score_sequence(np.zeros((3500, p.n_mels), np.float32), fp, cancel=cancel)
        else:
            score_recording(
                np.zeros(p.hop * 3500 + p.n_fft, np.float32), p.sample_rate, [fp], cancel=cancel
            )
    assert checks == 3


def test_threshold_excludes_a_bite_at_its_scored_frame():
    p = DEFAULT_MEL
    fp = Fingerprint("water", np.zeros((200, p.n_mels), np.float32))
    scores = np.full(1200, 0.2, np.float32)
    scores[1000] = 0.9
    report = bite_report({fp.name: scores}, [fp], p, separation_s=0.1)
    assert len(report.bites) == 1
    assert report.floor == pytest.approx(0.2)
    assert report.threshold == 0.5
    expected_onset = (1000 * p.hop + p.n_fft - fp.length * p.hop) / p.sample_rate
    assert report.bites[0].t == pytest.approx(expected_onset)
