"""Direct GUI entry point with a self-test usable from a windowed Windows EXE."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wowfish import APP_TITLE, __version__


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--version", action="store_true", help="显示版本")
    parser.add_argument(
        "--smoke-test", type=Path, metavar="REPORT", help="执行界面自检并写入 JSON 报告"
    )
    args = parser.parse_args()
    if args.version:
        if sys.stdout is not None:
            print(f"{APP_TITLE} {__version__}")
        return 0
    if args.smoke_test:
        from wowfish.gui.smoke import run_smoke

        return run_smoke(args.smoke_test)
    from loguru import logger

    from wowfish.cli import _add_missing_settings, _setup_logging
    from wowfish.gui.app import run
    from wowfish.messages import error_text

    path = _setup_logging()
    _add_missing_settings()
    try:
        run()
        return 0
    except Exception as e:
        logger.exception("无法启动中文界面")
        from tkinter import messagebox

        messagebox.showerror(APP_TITLE, f"无法打开界面：{error_text(str(e))}\n\n详细日志：{path}")
        return 1
    finally:
        logger.remove()


if __name__ == "__main__":
    raise SystemExit(main())
