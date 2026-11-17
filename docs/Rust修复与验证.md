# Rust 原生版完善与验证

版本：2.0.0-alpha.1。2026-11-17 接续 `rust-rewrite` 分支中的 Rust 工作区，修复配置与 Windows 编译问题，完善任务取消、自检、字体和发布资料。

## 本轮结果

Linux 上的 debug 与 release 配置均通过 **30 项 Rust 测试**；现有 Python v1.0.1 的 **89 项回归测试**全部通过。Rust 格式检查、Linux / Windows 后端的严格 Clippy 检查，以及 Python Ruff 与正式版发布元数据检查通过。

已生成 Linux release GUI 与 CLI，并运行生成的程序进行真实界面自检、PNG 校准和 WAV 分析。GUI 报告包含 15 张截图，覆盖 900×640、100% / 125% / 150% 字体缩放，以及设置与诊断页底部按钮。

本地环境为 Rust 1.97.0、Python 3.12.16；音频参考数据记录 NumPy 与 Python 的生成版本。Windows 原生链接、EXE 运行、音频设备和真实 Forever 客户端联调仍需 Windows 环境验证。新增 CI 最低 Rust 1.88 检查；该最低版本检查尚未在本机执行。

## 修复内容

| 问题 | 修改 | 验证 |
| --- | --- | --- |
| 精简 TOML 缺少字段时，保存访问不存在的索引而崩溃 | 在锁内比较修改前后的配置值，仅更新发生变化的字段；安全创建缺失区段，兼容行内表 | 精简配置、行内表、未知字段、中文注释、保持原始引号、并发更新 |
| Windows 文件锁竞争返回系统错误 33，未进入重试流程 | 根据 fs2 提供的平台锁竞争错误码识别占用，保留 5 秒重试期限 | 现有 8 线程并发递增 80 次的配置回归，由 Linux 与 Windows CI 执行 |
| `--config 中文配置.toml` 的父目录为空而无法创建 | 相对文件名使用当前目录创建锁与原子写入临时文件 | 真实 CLI 子进程在临时目录创建、读取和备份恢复中文配置 |
| Windows 枚举结果经裸指针回调后失去类型推断 | 显式声明窗口结果类型 | Windows MSVC 目标全部源文件与测试的交叉编译 / Clippy 检查 |
| Windows 接口返回值未处理，严格检查失败 | 明确处理消息队列与窗口显示返回值；快捷键线程启动失败时取消并等待退出 | Windows 后端严格 Clippy 检查；原生运行检查由 Windows CI 执行 |
| 任务取消后仍显示上一次任务提示 | 旧线程完全退出后报告取消完成；排队任务继续启动，停止可清除后续请求；音频关闭后更新来源状态 | 模拟旧线程延迟清理、丢弃延迟结果、排队等待、取消完成与停止请求回归 |
| 音频重连可能保留旧缓冲或旧指纹状态 | 断开时清空待处理音频；试听重新打开来源后重置检测器 | 源码检查与 Windows 后端编译；真实设备重连待联调 |
| 截图倒计时后可能截到遮挡游戏的窗口 | 截图前要求选中客户端仍在前台 | 源码检查；原生截图由 Windows 自检及实际环境验证 |
| 水花测试错误要求所有未对齐音频都接近 1.0 | 按 Python 原实现复算：第一段未对齐水花约 0.979478，第二段对齐水花约 1.0 | 固定参考值测试，独立 Python 特征与四种水花数值对照 |
| 自检创建报告目录过晚，并先访问用户目录 | 自检提前创建专用目录，直接初始化隔离路径；不依赖用户默认目录 | release GUI 在 `FOREVERFISH_HOME` 指向不可写 `/proc` 路径时仍成功自检 |
| 自检未覆盖页面底部按钮 | 在各字体档位滚动至设置、诊断页底部，检查控件完整可见并保存截图 | 15 个真实绘制与截图案例，包含中文字符检查及非空白画面检查 |
| 内置可变字体默认字重为 100，文字偏细 | 改用固定上游提交的 Noto Sans SC Regular 原文件，保留许可并记录 SHA256 | 中文字符与真实界面自检；字体从 17,772,300 字节降至 8,331,336 字节 |
| 打包引用的使用说明与发布说明缺失 | 补齐 Rust 使用说明、发布说明、来源与许可；打包前检查锁文件、清单和 EXE 版本 | 发布输入、锁文件版本、Windows 清单与素材一致性回归 |

原可变字体保存在本地忽略目录 `.audit/rust-font-backup/`；发布素材使用未修改的静态 Regular 字体。上游地址、固定提交和校验值见 `crates/foreverfish/assets/FONT_SOURCE.md`。

## 数值对照

`scripts/rust/generate_audio_parity.py` 使用维护中的 Python v1.0.1 音频实现生成参考 JSON，Rust 测试不调用 Python，也不使用自身的输出更新参考值。

- 四种内置片段各取三个位置的完整 40 个 log-mel 频带，允许 `2e-3` dB 的浮点误差。
- 指纹长度与归一化特征探针，允许 `2e-6` 的绝对误差。
- 四种带静音前后缀、未对齐 onset 的录音，比较每段素材峰值、咬钩分组、背景得分和建议阈值，得分允许 `2e-5` 的绝对误差，时间允许一帧步长。
- 流式重采样在不同分包边界下保持输出长度和数值，已有水花检测测试比较大块与小块输入的逐帧得分。

Rust 重采样持续保留分数位置，与 Python 离线重采样的端点策略不同；数值对照录音使用素材原采样率 22050 Hz。上述测试证明当前输入上的兼容性，不代表所有采样率和录音环境的结果完全相同。

## 可复现命令

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo clippy --workspace --all-targets --target x86_64-pc-windows-msvc --locked -- -D warnings
cargo test --workspace --locked
cargo test --workspace --release --locked
cargo build --workspace --release --locked
LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a ./target/release/ForeverFishing-Rust --smoke-test checks/rust-native/release/gui.json
./target/release/ForeverFishing-Rust-CLI verify-bite crates/foreverfish/assets/clips/fishingbobber_1.wav
```

交叉编译检查需要安装 `x86_64-pc-windows-msvc` 目标。Python 验证仍使用 `uv run pytest`、`uv run ruff check .`、`uv run ruff format --check .` 和 `uv run python scripts/check_release.py`。

本机报告位于 `checks/rust-native/`，包括 GUI JSON、截图、Python JUnit、CLI 环境诊断、WAV 分析与校准结果。可读摘要保存为 `docs/audit/rust-validation.json`；README 的 Rust 图片来自生成的 release 程序，未修改截图内容。

Windows 打包脚本使用 PowerShell 7，校验 EXE 的文件 / 产品版本与 CLI 版本，使用临时目录生成便携 ZIP。构建信息采用无 BOM UTF-8，包含版本、源提交、工作区是否有改动、编译器和打包时间。SHA256 只包含本次预期产物。Windows 发布流程还需由该平台实际执行。
