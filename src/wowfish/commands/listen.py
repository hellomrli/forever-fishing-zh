"""List devices or inspect water-splash scores, with streaming WAV recordings."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

import typer

from wowfish.capture import find_window
from wowfish.commands._common import fail, load_config_or_fail
from wowfish.messages import clip_label

BLOCK = 2205


def listen(
    list_devices: bool = typer.Option(False, "--list", help="列出音频回环设备后退出。"),
    device: str | None = typer.Option(
        None, "--device", help="设备 ID 或名称；game 表示游戏进程，默认使用配置。"
    ),
    title: str | None = typer.Option(
        None, "--title", help="游戏窗口标题；默认使用配置并自动查找中英文客户端。"
    ),
    source_file: Annotated[
        Path | None, typer.Option("--from", exists=True, dir_okay=False, help="读取 WAV 文件。")
    ] = None,
    record: Annotated[
        Path | None, typer.Option("--record", help="将收到的音频流式写入 WAV 文件。")
    ] = None,
    seconds: float = typer.Option(
        0.0, "--seconds", min=0.0, help="试听时长；0 表示直到 Ctrl-C 或文件结束。"
    ),
    threshold: float | None = typer.Option(
        None, "--threshold", min=0.001, max=1.0, help="临时使用这个水花阈值。"
    ),
) -> None:
    """试听实际音频来源，显示音量、水花得分和咬钩事件。"""
    from wowfish.audio import AudioChunk, devices
    from wowfish.audio._file import WavFileSource
    from wowfish.audio._recording import RecordingSource
    from wowfish.audio._threaded import ThreadedSource
    from wowfish.audio.detector import detector_for

    if list_devices:
        try:
            found = devices.list_loopback_devices()
        except Exception as e:
            typer.echo(f"无法获取音频设备：{e}")
            return
        if not found:
            typer.echo("未找到回环设备，请连接声音输出设备后重试。")
        for d in found:
            typer.echo(f"{'默认' if d.is_default else '    '} {d.name}  [{d.id}]")
        return

    cfg = load_config_or_fail()
    title = cfg.game.window_title if title is None else title
    detector = detector_for(cfg.audio, threshold=threshold)
    source: Any
    try:
        if source_file is not None:
            source = WavFileSource(source_file)
        else:
            selector = device if device is not None else cfg.audio.selector
            window = find_window(title) if selector == devices.GAME else None
            source = ThreadedSource(
                devices.open_capture(
                    selector,
                    pid=window.pid if window else None,
                    title=window.title if window else title,
                    fallback=cfg.audio.fallback_to_device,
                )
            )
        if record is not None:
            source = RecordingSource(source, record)
    except Exception as e:
        fail(str(e))

    frames_read, next_report = 0, 0.0
    started = time.monotonic()
    try:
        with source:
            typer.echo(
                f"实际音频来源：{getattr(source, 'name', source_file or '音频文件')}", err=True
            )
            reason = getattr(source, "fallback_reason", "")
            if reason:
                typer.echo(reason, err=True)
            while True:
                samples = source.read(BLOCK)
                if len(samples):
                    frames_read += len(samples)
                    t_end = frames_read / source.sample_rate
                    for event in detector.feed(AudioChunk(samples, source.sample_rate, t_end)):
                        typer.echo(
                            f"咬钩：{clip_label(event.clip)}，得分 {event.score:.2f}，"
                            f"时间 {event.t:.2f} 秒"
                        )
                    if t_end >= next_report:
                        scores = "  ".join(
                            f"{clip_label(s.name)}={max(0, s.score):.2f}"
                            for s in detector.last_scores
                        )
                        typer.echo(
                            f"音频 {t_end:6.1f} 秒，音量 {detector.level_dbfs:6.1f} dBFS  {scores}"
                        )
                        next_report += 0.25
                elapsed = (
                    frames_read / source.sample_rate if source_file else time.monotonic() - started
                )
                inner = source.inner if isinstance(source, RecordingSource) else source
                if (seconds and elapsed >= seconds) or getattr(inner, "exhausted", False):
                    break
                if source_file is None:
                    time.sleep(0.01)
    except KeyboardInterrupt:
        typer.echo("已停止", err=True)
    except Exception as e:
        fail(str(e))
    if record is not None:
        typer.echo(f"录音已保存：{record}")


def register(app: typer.Typer) -> None:
    app.command("listen")(listen)
