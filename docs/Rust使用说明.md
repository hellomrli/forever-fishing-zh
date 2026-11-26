# Rust 原生版使用说明

适用版本：2.0.0-alpha.5，原生桌面预览版。使用 Rust / egui 实现中文界面，内置中文字体、五种图标素材和四段咬钩声音，运行时无需安装 Python。

## 获取与启动

Windows 10 / 11 64 位支持游戏捕获、按键与 WASAPI 音频。Rust Native 工作流通过后，其 `ForeverFishing-Rust-Windows-x64` 构建产物包含图形版、命令行版、便携 ZIP、`BUILD_INFO.txt` 与 `SHA256SUMS.txt`。预览版与现有 Python 正式版使用不同的文件名。

解压后打开 `ForeverFishing-Rust.exe`。命令行操作使用 `ForeverFishing-Rust-CLI.exe`。完整便携包保留 `LICENSE`、`THIRD_PARTY_NOTICES.md` 和 `OFL-NotoSansSC.txt`。

从源码构建需要 Rust 1.88+，便携打包脚本使用 PowerShell 7。在项目根目录执行：

```powershell
cargo build --workspace --release --locked
./target/release/ForeverFishing-Rust.exe
./scripts/rust/package.ps1
```

生成的便携包位于 `dist/rust/`。Linux 可以运行中文界面、PNG 校准与离线 WAV 分析，游戏捕获、输入和设备试听由 Windows 后端提供。

## 第一次钓鱼

1. 在游戏里站到水边，开启自动拾取，将抛竿技能和“与目标互动”绑定为单键。
2. 执行 `/console SoftTargetIconGameObject 1`，让浮漂出现可识别的金色齿轮。
3. 在“钓鱼”页点击“刷新客户端”。仅有一个客户端时会自动选中，多个客户端时手动选择带 PID 的目标。
4. 在“设置”页填写抛竿键与互动键，点击“保存游戏设置”。默认抛竿键为 `1`，互动键为 `f`。
5. 在“校准与诊断”页点击“保存并试听”，切回游戏并手动钓鱼，查看实际声音来源与咬钩声音得分。完成后停止试听。
6. 点击“开始钓鱼”，切回选中的游戏窗口。图标尺寸尚未配置或不匹配时会尝试自动校准。

自动识别会核对窗口标题和游戏进程名或窗口类，不会仅因浏览器标题含有「Forever」就选中它。若改名的客户端未列出，在设置中填写该游戏窗口独有的标题片段并保存，再核对列表里的 PID。自定义标题也不会选中助手自身或已识别的浏览器窗口。

开始时先检查金色齿轮，已存在就继续等待。开启抛竿前跳跃时，每竿最多跳一次；落地后再抛，不会因概率设为 100% 而连续跳跃。实际抛竿后会等待 1.5 秒让浮漂落水，再开始识别；齿轮连续消失使用 1 秒防抖。识别到咬钩声音立即按拾取 / 起杆键；默认 30 秒没有咬钩声音就直接重新抛竿，即使齿轮仍在。30 秒从实际抛竿按键发送完成起计算。完整规则见 [简化钓鱼设计](简化钓鱼设计.md)。

**F8 暂停 / 继续，F9 停止。** 切出或最小化游戏会自动暂停，切回后先检查齿轮，存在就继续等待，不存在才抛竿；手动暂停需要手动继续。补饵等待被打断时，恢复后会重新使用补饵宏。

全局快捷键可以在设置中修改为 F1–F12，也可留空关闭。暂停键、停止键和游戏操作键不能冲突。配置校验或写入失败时，原快捷键继续有效。新快捷键被其他程序占用时，界面会显示注册失败，可更换后保存。

## 校准自己的浮漂

“校准金色齿轮”会观察最多 30 秒。点击后切回游戏，手动抛竿并等待金色齿轮出现；有效结果包含图标尺寸、得分及尺度区分。点击“保存校准”保存手动结果。

如果附近有其他玩家的浮漂，点击“截图并框选水面”，在 3 秒内切回选中的客户端，再在预览中拖动框选自己的浮漂落点。保存的区域使用窗口比例，随游戏窗口缩放。点击“恢复全窗口”可以撤销范围限制。

起杆由咬钩声音直接触发。图标匹配阈值和音频阈值可分别调整；声音命中后会跳过本轮图像扫描，直接发送拾取键。

收杆计数依据互动后图标连续消失，界面显示为“估计收杆”，不代表已确认鱼进入背包。

## 声音来源与录音验证

默认捕获选中游戏进程的声音。进程回环接口需要 Windows Build 20348+；打开失败时，默认回退到所选系统回环设备，并显示实际来源与失败原因。常见 Windows 10 客户端可使用系统回环。

系统回环读取输出设备的混合声音。选择“系统回环设备”，刷新并选择游戏实际使用的扬声器或耳机，保存并试听。设备名称来自 Windows，保留系统提供的语言。

点击“录音验证”后，在游戏里手动钓到一次咬钩。录音流式写入缓存中的 WAV，完成后分析咬钩声音、背景最高得分和建议阈值。点击“填入建议”后，还需点击“保存阈值”。没有足够清晰的咬钩声音时不会提供可应用的建议。

已有 WAV 可以填写文件路径，点击“分析 WAV”；支持 PCM 和 32 位 float WAV，并将多声道平均为单声道。分析分批执行，可点击“取消任务”。取消录音会保留已经写入的 WAV，便于再次分析。

试听、录音、校准和钓鱼使用同一套任务管理。切换任务时先关闭旧设备，再自动执行新请求。等待提示中的“取消后续操作”或 F9 可以取消排队请求。关闭界面也会先停止任务并释放设备。

## 配置和日志

默认复用 Python 正式版的 UTF-8 TOML 配置。Windows 通常位于：

```text
%LOCALAPPDATA%\ForeverFishing\ForeverFishing\config.toml
```

配置、缓存与日志的实际位置以界面及命令输出为准。可以给预览版设置单独的数据目录：

```powershell
$env:FOREVERFISH_HOME = Join-Path $PWD '.foreverfish-rust-home'
./target/release/ForeverFishing-Rust.exe
```

该目录下使用 `config/config.toml`、`cache/` 和 `log/`；兼容旧环境变量 `WOWFISH_HOME`。指定 `--config` 可以使用其他配置文件，支持相对路径和中文路径。

保存时保留注释、未知字段和未修改值的格式，并读取最新的校准结果。旧的画面确认、拾取确认、反应等待和跳过咬钩字段会被忽略；`run.cast_timeout_s` 继续控制无咬钩声音重抛时间，默认 30 秒。配置无效时会显示原因，修复文件或点击“备份并恢复默认配置”。恢复前的原文件保存为 `.toml.bak`，已有备份时递增编号。

“环境诊断”将配置、内置素材、设备及音频打开结果写入运行日志。“导出日志”会显示导出的完整路径。常驻日志达到 4 MiB 后会在下一次启动时轮换。

## 命令行

以下示例在 Windows 项目根目录执行：

```powershell
./target/release/ForeverFishing-Rust-CLI.exe --help
./target/release/ForeverFishing-Rust-CLI.exe windows
./target/release/ForeverFishing-Rust-CLI.exe devices
./target/release/ForeverFishing-Rust-CLI.exe config --path
./target/release/ForeverFishing-Rust-CLI.exe run --pid 12345
./target/release/ForeverFishing-Rust-CLI.exe listen --pid 12345 --seconds 30
./target/release/ForeverFishing-Rust-CLI.exe record recording.wav --pid 12345 --seconds 60
./target/release/ForeverFishing-Rust-CLI.exe verify-bite recording.wav
./target/release/ForeverFishing-Rust-CLI.exe calibrate --image screenshot.png --save
./target/release/ForeverFishing-Rust-CLI.exe watch screenshot.png --pid 12345
./target/release/ForeverFishing-Rust-CLI.exe doctor
./target/release/ForeverFishing-Rust-CLI.exe --config '中文配置.toml' config --restore-defaults
```

多客户端时用 `--pid` 绑定目标；命令行钓鱼同样支持 F8 / F9。录音和试听可用 Ctrl+C 取消。`doctor` 在能唯一确定客户端时尝试打开进程音频，多客户端环境可在图形界面选择后诊断。

## 界面自检

自检使用单独的数据目录，输出 JSON 和 15 张原始界面截图，覆盖三个页面、三个字体缩放以及设置和诊断页底部。Windows 还在自建测试窗口上验证窗口枚举、移动 / 缩放、最小化恢复、截图和快捷键消息。

```powershell
./target/release/ForeverFishing-Rust.exe --smoke-test checks/rust-gui.json
```

Linux 可执行：

```bash
LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a ./target/release/ForeverFishing-Rust --smoke-test checks/rust-gui.json
```

自检和离线回归不发送真实游戏操作键。当前预览版还需要实际 Forever 客户端的钓鱼联调；本地 Linux 验证不能代替 Windows 音频驱动与生成 EXE 的运行验证。
