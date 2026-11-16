# 来源与第三方说明

本项目由 [rsamf/wow-fishing-forever](https://github.com/rsamf/wow-fishing-forever) 的 MIT 源代码继续开发，参考提交为 `7e37aca73e018077087fe8bf34c05fd012babda4`。

保留原项目 `LICENSE` 中 Richard Franklin 的版权声明。`src/wowfish/assets/` 中的交互图标和水花音频沿用原项目素材，相关游戏内容和名称归原权利人。本项目与游戏运营方没有关联。

主要运行依赖为 Python / Tcl-Tk、NumPy、OpenCV、soundcard、ttkbootstrap / Pillow、mss、Typer、Loguru、platformdirs、tomlkit、tomli-w 和 filelock，各依赖保留其原有许可证。

Windows 单文件可执行程序通过 PyInstaller 构建。源码依赖和确切版本记录在 `pyproject.toml` 与 `uv.lock` 中；发行包同时携带本说明与 MIT 许可证。
