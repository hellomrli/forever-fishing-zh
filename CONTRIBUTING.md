# 开发与贡献

主分支 `main` 维护中文版本，包内部名称保留为 `wowfish`；发行项目名为 `forever-fishing-zh`。

```bash
uv sync --locked --group dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run foreverfish gui
```

界面自检需要图形桌面。Linux 虚拟显示可运行：

```bash
xvfb-run -a uv run python -m wowfish.gui.launcher --smoke-test checks/gui.json
```

Windows 原生构建：

```powershell
uv run pyinstaller --clean --noconfirm packaging/wowfish.spec
.\dist\ForeverFishing.exe --smoke-test checks\windows-exe.json
uv run python scripts/package_release.py
```

Windows 自检还会创建一个中文测试窗口，检查原生枚举、移动 / 缩放后的截图、最小化恢复和快捷键消息；不会发送游戏操作键。
单元测试使用内置素材和模拟输入，无需运行游戏或连接音频设备。

界面只在 Tk 主线程更新；COM 音频设备必须在自己的读取线程打开和释放。保存配置请使用 `update_config` 读取最新状态并修改目标字段，避免写入过期的整个配置快照。

发布时更新 `pyproject.toml`、`uv.lock`、`src/wowfish/__init__.py` 的备用版本、`packaging/windows_version.txt`、`packaging/windows.manifest` 及 `docs/releases/vX.Y.Z.md` 说明。`scripts/check_release.py` 检查版本一致性、素材和发布说明。待 CI 通过后创建对应 `vX.Y.Z` 标签；Release Windows 工作流会原生构建、自检、生成校验值并发布到 GitHub Releases。
