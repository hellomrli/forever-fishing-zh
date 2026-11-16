"""`wowfish calibrate SCREENSHOT`: measure the interact icon's on-screen size once."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from wowfish import paths
from wowfish.commands._common import fail, load_config_or_fail
from wowfish.config import update_config


def calibrate(
    screenshot: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, help="带有浮漂金色齿轮的游戏截图。"),
    ],
    save: bool = typer.Option(False, "--save", help="将校准结果保存到配置文件。"),
) -> None:
    """在截图中校准金色齿轮的大小、位置和识别阈值。"""

    from wowfish.vision.calibrate import calibrate_icon_scale
    from wowfish.vision.imageio import read_image
    from wowfish.vision.template import load_icon_templates

    cfg = load_config_or_fail()
    frame = read_image(screenshot)
    if frame is None:
        fail(f"无法读取图片：{screenshot}")
    cal = calibrate_icon_scale(frame, load_icon_templates(), match_width=cfg.vision.match_width)
    h, w = frame.shape[:2]
    typer.echo(
        f"icon_size_px={cal.icon_size_px} native={cal.native_size} score={cal.score:.3f} "
        f"at ({cal.x}, {cal.y}) in {w}x{h}"
    )
    threshold = cal.suggested_threshold()
    typer.echo(f"建议图标阈值：{threshold:.2f}")
    if not cal.valid:
        typer.echo(
            "提示：截图中未找到金色齿轮，请让浮漂处于互动距离内，并保持交互图标可见。",
            err=True,
        )
        if save:
            typer.echo("校准未保存", err=True)
            raise typer.Exit(code=1)
    if save:
        update_config(
            lambda current: replace(
                current,
                vision=replace(
                    current.vision,
                    icon_size_px=cal.icon_size_px,
                    icon_native_size=cal.native_size,
                    icon_threshold=threshold,
                ),
            )
        )
        typer.echo(f"已保存到：{paths.config_file()}")


def register(app: typer.Typer) -> None:
    app.command("calibrate")(calibrate)
