# 来源与第三方说明

本项目由 [rsamf/wow-fishing-forever](https://github.com/rsamf/wow-fishing-forever) 的 MIT 源代码继续开发，参考提交为 `7e37aca73e018077087fe8bf34c05fd012babda4`。

2026-11-22 继续评审上游至 `97653ba0566d2e1c14b70848888c76f674f7655e`，参考其浮漂落水等待、抛竿前图标核对、PipeWire 客户端 PID 与子进程音频选择。采用记录见 [持续优化记录](docs/持续优化.md)。

保留原项目 `LICENSE` 中 Richard Franklin 的版权声明。`src/wowfish/assets/` 中的交互图标和水花音频沿用原项目素材，相关游戏内容和名称归原权利人。本项目与游戏运营方没有关联。

主要运行依赖为 Python / Tcl-Tk、NumPy、OpenCV、soundcard、ttkbootstrap / Pillow、mss、Typer、Loguru、platformdirs、tomlkit、tomli-w 和 filelock，各依赖保留其原有许可证。

Windows 单文件可执行程序通过 PyInstaller 构建。源码依赖和确切版本记录在 `pyproject.toml` 与 `uv.lock` 中；发行包同时携带本说明与 MIT 许可证。

Rust 原生版本的图标和音频素材位于 `crates/foreverfish/assets/`，沿用相同的上游素材。主要依赖为 Rust 标准库、eframe / egui / wgpu、windows-rs、image、rustfft、hound、clap、serde、toml / toml_edit、directories、fs2、rand、tempfile、anyhow 和 ctrlc；确切版本记录在 `Cargo.lock` 中。

Rust 界面内置 **Noto Sans SC Regular** 中文字体，原版权为 Adobe，采用 SIL Open Font License 1.1。字体使用 [Noto CJK 上游](https://github.com/notofonts/noto-cjk)的静态 Regular 原文件；固定提交、下载地址与 SHA256 记录在 `crates/foreverfish/assets/FONT_SOURCE.md`，许可证全文位于同目录的 `OFL-NotoSansSC.txt`。Rust 便携包同时携带来源记录和字体许可证。
