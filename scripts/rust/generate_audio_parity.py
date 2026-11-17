"""Regenerate Rust audio reference values with the maintained Python implementation.

Run with `uv run python scripts/rust/generate_audio_parity.py` from the repository.
Rust tests read the checked-in JSON and do not need Python or audio devices.
"""

from __future__ import annotations

import json
import platform
from pathlib import Path

import numpy as np

from wowfish.audio.clips import build_fingerprints, load_reference_clips
from wowfish.audio.features import DEFAULT_MEL, log_mel
from wowfish.audio.verify import bite_report, clip_peaks, score_recording

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "crates/foreverfish/tests/fixtures/python_audio.json"


def main() -> None:
    p = DEFAULT_MEL
    fingerprints = build_fingerprints()
    clips = load_reference_clips()
    cases = []
    for i, (name, samples) in enumerate(clips.items()):
        padding = p.sample_rate + i * 13
        recording = np.concatenate(
            [np.zeros(padding, np.float32), samples, np.zeros(p.sample_rate * 2, np.float32)]
        )
        scores = score_recording(recording, p.sample_rate, fingerprints)
        report = bite_report(scores, fingerprints, p)
        cases.append(
            {
                "clip": name,
                "padding": padding,
                "duration_s": len(recording) / p.sample_rate,
                "peaks": [
                    {"clip": peak.name, "score": peak.score, "onset_s": peak.t}
                    for peak in clip_peaks(scores, fingerprints, p)
                ],
                "bites": [
                    {"clip": bite.name, "score": bite.score, "onset_s": bite.t}
                    for bite in report.bites
                ],
                "floor": report.floor,
                "threshold": report.threshold,
            }
        )
    probes = []
    for fp in fingerprints:
        features = log_mel(clips[fp.name], p)
        probes.append(
            {
                "clip": fp.name,
                "frames": [
                    {"index": index, "bands": features[index].tolist()} for index in (0, 7, 100)
                ],
                "fingerprint_length": fp.length,
                "fingerprint_probes": [
                    {"frame": frame, "band": band, "value": float(fp.feats[frame, band])}
                    for frame, band in ((0, 0), (7, 20), (100, 39))
                ],
            }
        )
    fixture = {
        "reference": "src/wowfish/audio (Python v1.0.1)",
        "python": platform.python_version(),
        "numpy": np.__version__,
        "sample_rate": p.sample_rate,
        "fft_size": p.n_fft,
        "hop": p.hop,
        "bands": p.n_mels,
        "probes": probes,
        "recordings": cases,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
