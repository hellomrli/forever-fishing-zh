"""`wowfish gui`: the setup and fishing window."""

from __future__ import annotations

import typer

from wowfish.commands._common import fail


def gui() -> None:
    """打开中文界面，进行钓鱼、设置、校准和诊断。"""
    try:
        import tkinter  # noqa: F401

        import ttkbootstrap  # noqa: F401
    except ImportError as e:
        fail(f"界面需要 Tk 和 ttkbootstrap，请安装相关组件：{e}")
    from wowfish.gui.app import run

    try:
        run()
    except Exception as e:  # e.g. no display
        fail(f"无法打开界面：{e}")


def register(app: typer.Typer) -> None:
    app.command("gui")(gui)
