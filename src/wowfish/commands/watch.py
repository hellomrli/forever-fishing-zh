"""Visual diagnostics over image files or a bound live game window."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import numpy as np
import typer

from wowfish.commands._common import fail, load_config_or_fail
from wowfish.vision.imageio import read_image, write_image

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}


def _file_frames(source: Path, fps: float) -> Iterator[tuple[float, np.ndarray, float]]:
    files = (
        sorted(p for p in source.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
        if source.is_dir()
        else [source]
    )
    if not files:
        fail(f"目录中没有图片：{source}")
    for i, path in enumerate(files):
        frame = read_image(path)
        if frame is None:
            fail(f"无法读取图片：{path}")
        yield i / fps, frame, 0.0


def _live_frames(
    title: str, fps: float, seconds: float
) -> Iterator[tuple[float, np.ndarray, float]]:
    from wowfish import capture

    window = capture.find_window(title)
    if window is None:
        fail("未找到目标游戏窗口，请启动游戏后检查窗口标题")
    cap = capture.get_capturer(window)
    try:
        cap.open(window)
        typer.echo(
            f"捕获 {window.title}，{window.width} × {window.height}，{fps:g} 帧/秒", err=True
        )
        start = time.perf_counter()
        while not seconds or time.perf_counter() - start < seconds:
            current = capture.refresh_window(window)
            if current is None:
                fail("目标游戏窗口已关闭")
            window = current
            cap.update_window(window)
            if window.minimized:
                time.sleep(0.1)
                continue
            grabbed = time.perf_counter()
            frame = cap.grab()
            yield grabbed - start, frame, (time.perf_counter() - grabbed) * 1000
            delay = 1.0 / fps - (time.perf_counter() - grabbed)
            if delay > 0:
                time.sleep(delay)
    finally:
        cap.close()


def watch(
    source: Annotated[
        Path | None, typer.Option("--from", exists=True, help="截图文件或图片目录。")
    ] = None,
    live: bool = typer.Option(False, "--live", help="捕获游戏窗口。"),
    fps: float = typer.Option(10.0, "--fps", min=0.1, help="每秒帧数。"),
    seconds: float = typer.Option(0.0, "--seconds", min=0.0, help="运行时长；0 表示直到 Ctrl-C。"),
    save_frames: Annotated[
        Path | None, typer.Option("--save-frames", help="保存带识别标注的预览图。")
    ] = None,
    title: str | None = typer.Option(None, "--title", help="窗口标题，默认使用配置并自动查找。"),
) -> None:
    """检查图标识别的得分、位置和识别阶段。"""
    from wowfish.vision.annotate import annotate_match
    from wowfish.vision.calibrate import calibrate_icon_scale
    from wowfish.vision.matcher import IconMatcher
    from wowfish.vision.region import Region
    from wowfish.vision.search import IconSearch
    from wowfish.vision.template import load_icon_templates, template_for_size

    if (source is None) == (not live):
        fail("请选择 --from 图片路径 或 --live，且只能使用其中一项")
    cfg = load_config_or_fail()
    title = cfg.game.window_title if title is None else title
    v = cfg.vision
    region = Region(v.search_left, v.search_top, v.search_right, v.search_bottom)
    templates = load_icon_templates()
    frames = _live_frames(title, fps, seconds) if live else _file_frames(source, fps)
    search = None
    try:
        for i, (t, frame, grab_ms) in enumerate(frames):
            started = time.perf_counter()
            if search is None:
                size = v.icon_size_px
                if size == 0:
                    x0, y0, x1, y1 = region.to_box(frame.shape[1], frame.shape[0])
                    cal = calibrate_icon_scale(
                        frame[y0:y1, x0:x1], templates, match_width=v.match_width
                    )
                    cal = replace(cal, x=cal.x + x0, y=cal.y + y0)
                    if not cal.valid:
                        typer.echo(f"{t:.2f} 秒：未找到金色齿轮，继续等待")
                        continue
                    size = cal.icon_size_px
                    typer.echo(f"自动识别图标：{size} 像素，得分 {cal.score:.2f}")
                search = IconSearch(
                    IconMatcher(template_for_size(templates, size), v.match_width),
                    v.icon_threshold,
                    allowed_region=region,
                )
            phase, match = search.step(frame)
            ms = grab_ms + (time.perf_counter() - started) * 1000
            label = "跟踪" if phase == "track" else "寻找"
            found = "找到" if match.accepted(v.icon_threshold) else "未找到"
            typer.echo(
                f"{t:7.2f} 秒  {label}  得分 {match.score:.2f}  "
                f"位置 ({match.x}, {match.y})  {found}  {ms:.1f} ms"
            )
            if save_frames:
                write_image(
                    save_frames / f"frame_{i:05d}.jpg",
                    annotate_match(frame, match, threshold=v.icon_threshold),
                )
    except KeyboardInterrupt:
        typer.echo("已停止", err=True)
    except Exception as e:
        fail(str(e))


def register(app: typer.Typer) -> None:
    app.command("watch")(watch)
