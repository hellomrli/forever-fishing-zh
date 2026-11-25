"""`wowfish verify-bite RECORDING.wav`: do the bites stand out, and what threshold to use."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from wowfish import paths
from wowfish.commands._common import load_config_or_fail
from wowfish.config import update_config

MIN_MARGIN = 0.15


def verify_bite(
    recording: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, help="listen --record 保存的 WAV 录音。"),
    ],
    save: bool = typer.Option(False, "--save", help="将建议的咬钩声音阈值保存到配置文件。"),
) -> None:
    """分析咬钩声音录音，给出得分和阈值建议。"""
    from wowfish.audio.clips import build_fingerprints
    from wowfish.audio.features import MelParams
    from wowfish.audio.verify import bite_report, clip_peaks, score_recording
    from wowfish.audio.wav import read_wav

    load_config_or_fail()
    p = MelParams()
    fps = build_fingerprints(p)
    y, rate = read_wav(recording)
    scores = score_recording(y, rate, fps, p)
    typer.echo(f"{recording}: {len(y) / rate:.1f}s at {rate} Hz")
    peaks = clip_peaks(scores, fps, p)
    for cs in peaks:
        typer.echo(f"{cs.name:16s} peak {cs.score:5.2f} at {cs.t:6.2f}s")
    best = max(peaks, key=lambda c: c.score)
    report = bite_report(scores, fps, p)
    threshold = report.threshold
    if report.stands_out:
        lo = min(b.score for b in report.bites)
        hi = max(b.score for b in report.bites)
        when = ", ".join(f"{b.t:.1f}s ({b.name})" for b in sorted(report.bites, key=lambda b: b.t))
        typer.echo(f"{len(report.bites)} bite(s) scoring {lo:.2f}-{hi:.2f} at {when}")
        typer.echo(f"everything else scores at most {report.floor:.2f}")
    typer.echo(f"建议咬钩声音阈值：{threshold:.2f}")
    if not report.stands_out:
        typer.echo(
            f"提示：没有清晰的咬钩声音，最高得分 {best.score:.2f}，背景最高 {report.floor:.2f}；"
            "请调低音乐和环境音，重新录制一次咬钩。",
            err=True,
        )
        if save:
            typer.echo("阈值未保存", err=True)
            raise typer.Exit(code=1)
    if save:
        update_config(
            lambda current: replace(current, audio=replace(current.audio, bite_threshold=threshold))
        )
        typer.echo(f"已保存到：{paths.config_file()}")


def register(app: typer.Typer) -> None:
    app.command("verify-bite")(verify_bite)
