use crate::{
    audio::{self, Cancel},
    config::Store,
    paths::Paths,
    platform::{self, Hotkey},
    runner::{Event, Manager, Task},
    vision::{Matcher, Region},
};
use anyhow::{ensure, Context, Result};
use clap::{Parser, Subcommand};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

#[derive(Parser)]
#[command(name = "foreverfish-rust", version = crate::VERSION, about = "Forever 钓鱼助手：原生 Rust 中文桌面版")]
struct Arguments {
    #[arg(long, global = true, help = "指定 UTF-8 TOML 配置路径")]
    config: Option<PathBuf>,
    #[arg(long, help = "启动真实界面自检并输出 JSON 报告")]
    smoke_test: Option<PathBuf>,
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    #[command(about = "打开中文图形界面（默认）")]
    Gui,
    #[command(about = "显示配置或路径，支持备份恢复")]
    Config {
        #[arg(long)]
        path: bool,
        #[arg(long)]
        restore_defaults: bool,
    },
    #[command(about = "列出中英文游戏客户端")]
    Windows,
    #[command(about = "列出系统回环设备")]
    Devices,
    #[command(about = "检查配置、素材、窗口和实际音频来源")]
    Doctor,
    #[command(about = "运行钓鱼流程；F8 暂停 / 继续，F9 停止")]
    Run {
        #[arg(long, help = "目标客户端进程 PID，多客户端时必填")]
        pid: Option<u32>,
    },
    #[command(about = "试听并显示水花得分")]
    Listen {
        #[arg(long)]
        pid: Option<u32>,
        #[arg(long, default_value_t = 0)]
        seconds: u32,
    },
    #[command(about = "流式录制 WAV 并分析水花")]
    Record {
        output: PathBuf,
        #[arg(long)]
        pid: Option<u32>,
        #[arg(long, default_value_t = 60)]
        seconds: u32,
    },
    #[command(name = "verify-bite", about = "离线分析 WAV，给出水花峰值与建议阈值")]
    VerifyBite { file: PathBuf },
    #[command(about = "从游戏或 PNG 图像校准金色齿轮")]
    Calibrate {
        #[arg(long)]
        image: Option<PathBuf>,
        #[arg(long)]
        pid: Option<u32>,
        #[arg(long)]
        save: bool,
    },
    #[command(about = "将目标客户端画面保存为 PNG")]
    Watch {
        output: PathBuf,
        #[arg(long)]
        pid: Option<u32>,
    },
}

pub fn main() -> Result<()> {
    let args = Arguments::parse();
    if let Some(path) = args.smoke_test {
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(std::path::Path::new("."));
        std::fs::create_dir_all(parent).context("无法创建自检报告目录")?;
        let paths = Paths::in_root(parent.join("rust-smoke-home"))?;
        crate::gui::run(paths, None, Some(path.clone()))?;
        let report: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).context("自检报告未生成")?)?;
        ensure!(report["ok"] == true, "界面自检失败：{}", report["errors"]);
        return Ok(());
    }
    let paths = Paths::discover()?;
    let store = Store::new(args.config.clone().unwrap_or_else(|| paths.config.clone()));
    let command = args.command.unwrap_or(Command::Gui);
    if matches!(command, Command::Gui) {
        return crate::gui::run(paths, args.config, None);
    }
    store.ensure()?;
    if let Command::Config {
        path,
        restore_defaults,
    } = command
    {
        if path {
            println!("{}", store.path.display());
        } else if restore_defaults {
            println!("备份：{}", store.recover()?.display());
        } else {
            println!("{}", std::fs::read_to_string(&store.path)?);
        }
        return Ok(());
    }
    let config = store.load()?;
    let select = |pid: Option<u32>| -> Result<platform::Window> {
        let mut windows = platform::windows(&config.game.window_title)?;
        if let Some(pid) = pid {
            windows.retain(|w| w.pid == pid);
        }
        ensure!(
            !windows.is_empty(),
            "未找到游戏窗口，请启动客户端并检查标题或 PID"
        );
        ensure!(
            windows.len() == 1,
            "找到多个客户端，请用 --pid 指定目标进程：{}",
            windows
                .iter()
                .map(|w| w.label())
                .collect::<Vec<_>>()
                .join("；")
        );
        Ok(windows.remove(0))
    };
    match command {
        Command::Windows => println!(
            "{}",
            serde_json::to_string_pretty(&platform::windows(&config.game.window_title)?)?
        ),
        Command::Devices => println!("{}", serde_json::to_string_pretty(&platform::devices()?)?),
        Command::VerifyBite { file } => println!(
            "{}",
            serde_json::to_string_pretty(&audio::analyze_file(&file, &Cancel::default())?)?
        ),
        Command::Watch { output, pid } => {
            platform::capture(&select(pid)?, Region::default())?.save(&output)?;
            println!("截图：{}", output.display());
        }
        Command::Calibrate {
            image: Some(image),
            save,
            ..
        } => {
            let frame = image::open(&image)
                .with_context(|| format!("无法读取 PNG {}", image.display()))?
                .into_rgb8();
            let cal = Matcher::new(config.vision.match_width)?.calibrate(
                &frame,
                Region::from_config(&config.vision),
                &Cancel::default(),
            )?;
            ensure!(cal.valid(), "未找到具有尺度区分的金色齿轮：{cal:?}");
            if save {
                store.update(|cfg| {
                    cfg.vision.icon_size_px = cal.icon.size;
                    cfg.vision.icon_native_size = cal.native_size;
                    cfg.vision.icon_threshold = cal.threshold();
                })?;
            }
            println!("{}", serde_json::to_string_pretty(&cal)?);
        }
        command => {
            let (task, seconds, save_calibration) = match command {
                Command::Run { pid } => (
                    Task::Fishing {
                        config: config.clone(),
                        window: select(pid)?,
                        store: store.clone(),
                    },
                    0,
                    false,
                ),
                Command::Listen { pid, seconds } => (
                    Task::Listen {
                        config: config.clone(),
                        window: if config.audio.capture == "game" {
                            Some(select(pid)?)
                        } else {
                            None
                        },
                    },
                    seconds,
                    false,
                ),
                Command::Record {
                    output,
                    pid,
                    seconds,
                } => (
                    Task::Record {
                        config: config.clone(),
                        window: if config.audio.capture == "game" {
                            Some(select(pid)?)
                        } else {
                            None
                        },
                        seconds,
                        path: output,
                    },
                    0,
                    false,
                ),
                Command::Calibrate { pid, save, .. } => (
                    Task::Calibrate {
                        config: config.clone(),
                        window: select(pid)?,
                    },
                    0,
                    save,
                ),
                Command::Doctor => (
                    Task::Doctor {
                        config: config.clone(),
                        window: select(None).ok(),
                    },
                    0,
                    false,
                ),
                _ => unreachable!(),
            };
            let quit = Arc::new(AtomicBool::new(false));
            let signal = quit.clone();
            ctrlc::set_handler(move || signal.store(true, Ordering::Release))?;
            let hotkeys = if task.fishing() {
                Some(platform::Hotkeys::new(&config.game)?)
            } else {
                None
            };
            let mut manager = Manager::new();
            manager.request(task);
            let started = Instant::now();
            let mut failed = None;
            while manager.busy() {
                if quit.load(Ordering::Acquire)
                    || (seconds > 0 && started.elapsed().as_secs() >= seconds as u64)
                {
                    manager.stop();
                }
                if let Some(keys) = &hotkeys {
                    for key in keys.poll() {
                        match key {
                            Hotkey::Pause => manager.toggle_pause(),
                            Hotkey::Stop => manager.stop(),
                        }
                    }
                }
                for event in manager.poll() {
                    match event {
                        Event::Message(message) => eprintln!("{message}"),
                        Event::Cancelled(name) => eprintln!("已取消{name}"),
                        Event::Error(error) => {
                            eprintln!("{error}");
                            failed = Some(error);
                        }
                        Event::Source { name, fallback } => {
                            eprintln!("实际音频来源：{name}\n{fallback}")
                        }
                        Event::Snapshot { state, stats, .. } => eprintln!(
                            "{} · 抛竿 {} · 估计收杆 {} · 未成功 {}",
                            state.label(),
                            stats.casts,
                            stats.catches,
                            stats.misses
                        ),
                        Event::Recorded { report, path } => {
                            eprintln!("WAV：{}", path.display());
                            println!("{}", serde_json::to_string_pretty(&report)?);
                        }
                        Event::Verified(report) => {
                            println!("{}", serde_json::to_string_pretty(&report)?)
                        }
                        Event::Calibrated(cal) => {
                            if save_calibration {
                                store.update(|cfg| {
                                    cfg.vision.icon_size_px = cal.icon.size;
                                    cfg.vision.icon_native_size = cal.native_size;
                                    cfg.vision.icon_threshold = cal.threshold();
                                })?;
                            }
                            println!("{}", serde_json::to_string_pretty(&cal)?);
                        }
                        Event::Doctor(report) => {
                            println!("{}", serde_json::to_string_pretty(&report)?)
                        }
                        _ => {}
                    }
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            if let Some(error) = failed {
                anyhow::bail!("{error}");
            }
        }
    }
    Ok(())
}
