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

## Rust 原生版本

`rust-rewrite` 分支的 Rust 工作区位于 `crates/foreverfish`。需要 Rust 1.88+，桌面界面使用 eframe / egui；Windows 截图、输入与 WASAPI 音频接口在 `src/platform` 中实现。Linux 可验证通用算法、配置、任务管理、界面和离线命令。

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo build --workspace --release --locked
```

Linux 界面自检与 Windows 后端编译检查：

```bash
LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a ./target/release/ForeverFishing-Rust --smoke-test checks/rust-gui.json
rustup target add x86_64-pc-windows-msvc
cargo check --workspace --all-targets --target x86_64-pc-windows-msvc --locked
```

交叉编译检查验证 Windows 源码的类型，不替代 Windows 原生链接、生成 EXE 和接口自检。Windows 使用 `scripts/rust/package.ps1` 打包，完整流程由 `.github/workflows/rust.yml` 执行。

游戏状态机 `engine.rs` 不直接读取设备或发送按键；等待使用单调时钟的截止时间，保持暂停和停止可响应。界面只在 egui 主线程处理事件。音频打开、读取和释放必须由同一工作线程完成；切换任务必须等旧线程及其音频资源完全退出。取消的任务只报告清理完成，不再提交校准、录音或设备结果。

配置使用 `Store::update` 在文件锁内读取最新内容，只修改目标字段；保留中文、注释、未知字段和未修改值的原始格式。不要直接将界面中的整个配置快照覆盖到磁盘。

音频数值测试读取 `crates/foreverfish/tests/fixtures/python_audio.json` 中的 Python 参考数据。修改共同算法时，在确认 Python 行为正确后运行 `uv run python scripts/rust/generate_audio_parity.py` 更新，并检查参考值变化。Rust 测试与运行不依赖 Python。

Rust 发布版本定义在根 `Cargo.toml` 的 `workspace.package.version` 中，还需更新 `Cargo.lock`、`crates/foreverfish/windows.manifest` 的数字版本，以及 `docs/releases/rust-vX.Y.Z.md`。预览标签为 `rust-vX.Y.Z`，与 Python 的 `vX.Y.Z` 标签分别使用各自的发布流程。

## 跟踪上游

`.github/upstream.json` 记录已评审的上游提交，采用与验证记录位于 [持续优化记录](docs/持续优化.md)。检查只需要 Python 3.12+ 的标准库：

```bash
python3 scripts/check_upstream.py --fail-on-changes
```

报告写入 `checks/upstream/upstream.json` 和 `upstream.md`。退出码 0 表示已评审到当前上游提交，1 表示存在待评审变化，2 表示请求、配置或报告写入失败。脚本先解析上游分支的固定提交，再生成比较报告。

先对照变更文件确认功能与兼容性，补充有意义的回归验证并更新采用记录，再修改 `reviewed_commit` 和 `reviewed_at`。`Upstream Review` 工作流在默认分支每日运行，也支持手动触发；未评审变化会使检查失败，并上传差异报告。Python CI 同时覆盖 `main` 与 `rust-rewrite`，便于验证两个实现的共同修改。
