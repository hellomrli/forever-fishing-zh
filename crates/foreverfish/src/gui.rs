use crate::{
    audio::{Device, Report},
    config::{Config, Store},
    engine::{State, Stats},
    paths::Paths,
    platform::{self, Hotkey, Hotkeys, Window},
    runner::{Event, Manager, Task},
    smoke::Smoke,
    vision::{Calibration, Frame, IconMatch, Region},
};
use anyhow::{Context, Result};
use eframe::egui::{
    self, Color32, FontFamily, FontId, Pos2, Rect, RichText, Sense, Stroke, TextureHandle, Vec2,
};
use std::{
    collections::VecDeque,
    fs::{self, File, OpenOptions},
    io::{BufWriter, Write},
    path::PathBuf,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub struct App {
    pub(crate) page: usize,
    pub(crate) config: Config,
    pub(crate) config_error: Option<String>,
    store: Store,
    paths: Paths,
    manager: Manager,
    hotkeys: Option<Hotkeys>,
    windows: Vec<Window>,
    selected: Option<(usize, u32)>,
    devices: Vec<Device>,
    state: State,
    stats: Stats,
    manual_paused: bool,
    source: String,
    fallback: String,
    message: String,
    error: Option<String>,
    preview: Option<TextureHandle>,
    preview_icon: Option<IconMatch>,
    graph: VecDeque<Vec<f32>>,
    level: f32,
    report: Option<Report>,
    calibration: Option<Calibration>,
    logs: VecDeque<String>,
    journal: BufWriter<File>,
    wav_path: String,
    recording_seconds: u32,
    screenshot: Option<TextureHandle>,
    region_open: bool,
    selection: Option<Region>,
    drag_start: Option<Pos2>,
    pub(crate) font_scale: f32,
    smoke: Option<Smoke>,
    closing: bool,
    pub(crate) smoke_scroll_bottom: bool,
    pub(crate) controls: Vec<(String, Rect)>,
}

pub fn run(paths: Paths, config_path: Option<PathBuf>, smoke: Option<PathBuf>) -> Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size(if smoke.is_some() {
                [900.0, 640.0]
            } else {
                [1060.0, 780.0]
            })
            .with_min_inner_size([900.0, 640.0]),
        #[cfg(windows)]
        renderer: eframe::Renderer::Wgpu,
        ..Default::default()
    };
    eframe::run_native(
        crate::TITLE,
        options,
        Box::new(move |context| Ok(Box::new(App::new(context, paths, config_path, smoke)?))),
    )
    .map_err(|error| anyhow::anyhow!("界面启动失败：{error}"))
}

impl App {
    fn new(
        context: &eframe::CreationContext<'_>,
        paths: Paths,
        config_path: Option<PathBuf>,
        smoke: Option<PathBuf>,
    ) -> Result<Self> {
        let mut fonts = egui::FontDefinitions::default();
        fonts.font_data.insert(
            "Chinese".into(),
            egui::FontData::from_static(crate::assets::FONT).into(),
        );
        for family in [FontFamily::Proportional, FontFamily::Monospace] {
            fonts
                .families
                .entry(family)
                .or_default()
                .insert(0, "Chinese".into());
        }
        context.egui_ctx.set_fonts(fonts);
        context.egui_ctx.set_visuals(egui::Visuals::dark());
        let mut style = (*context.egui_ctx.style()).clone();
        style.spacing.item_spacing = Vec2::new(10.0, 9.0);
        style.spacing.button_padding = Vec2::new(14.0, 7.0);
        style
            .text_styles
            .insert(egui::TextStyle::Body, FontId::proportional(16.0));
        style
            .text_styles
            .insert(egui::TextStyle::Button, FontId::proportional(16.0));
        style
            .text_styles
            .insert(egui::TextStyle::Heading, FontId::proportional(24.0));
        context.egui_ctx.set_style(style);
        let store = Store::new(config_path.unwrap_or_else(|| paths.config.clone()));
        store.ensure()?;
        let (config, config_error) = match store.load() {
            Ok(config) => (config, None),
            Err(error) => (Config::default(), Some(format!("{error:#}"))),
        };
        let log_path = paths.logs.join("foreverfish-rust.log");
        if fs::metadata(&log_path).is_ok_and(|m| m.len() > 4 * 1024 * 1024) {
            fs::rename(&log_path, paths.logs.join("foreverfish-rust.previous.log"))?;
        }
        let journal = BufWriter::new(
            OpenOptions::new()
                .create(true)
                .append(true)
                .open(log_path)?,
        );
        let mut app = Self {
            page: 0,
            config,
            config_error,
            store,
            paths,
            manager: Manager::new(),
            hotkeys: None,
            windows: vec![],
            selected: None,
            devices: vec![],
            state: State::Stopped,
            stats: Stats::default(),
            manual_paused: false,
            source: "尚未打开音频".into(),
            fallback: String::new(),
            message: "选择客户端，确认游戏键位后开始".into(),
            error: None,
            preview: None,
            preview_icon: None,
            graph: VecDeque::new(),
            level: -120.0,
            report: None,
            calibration: None,
            logs: VecDeque::new(),
            journal,
            wav_path: String::new(),
            recording_seconds: 60,
            screenshot: None,
            region_open: false,
            selection: None,
            drag_start: None,
            font_scale: 1.0,
            smoke: smoke.map(Smoke::new),
            closing: false,
            smoke_scroll_bottom: false,
            controls: vec![],
        };
        app.refresh_windows();
        if app.smoke.is_none() {
            app.register_hotkeys();
        }
        app.log(format!(
            "Rust {} 已启动，配置：{}",
            crate::VERSION,
            app.store.path.display()
        ));
        Ok(app)
    }

    fn log(&mut self, message: impl Into<String>) {
        let text = message.into();
        let line = format!("[{}] {text}", timestamp());
        let _ = writeln!(self.journal, "{line}");
        let _ = self.journal.flush();
        self.logs.push_back(line);
        if self.logs.len() > 1500 {
            self.logs.pop_front();
        }
    }
    fn feedback(&mut self, result: Result<()>) {
        if let Err(error) = result {
            self.error = Some(format!("{error:#}"));
            self.log(format!("错误：{error:#}"));
        }
    }
    fn selected_window(&self) -> Option<Window> {
        self.windows
            .iter()
            .find(|w| Some((w.handle, w.pid)) == self.selected)
            .cloned()
    }
    fn require_window(&self) -> Result<Window> {
        self.selected_window().context("请启动游戏并选择一个客户端")
    }
    fn refresh_windows(&mut self) {
        match platform::windows(&self.config.game.window_title) {
            Ok(windows) => {
                if !windows
                    .iter()
                    .any(|w| Some((w.handle, w.pid)) == self.selected)
                {
                    self.selected = if windows.len() == 1 {
                        Some((windows[0].handle, windows[0].pid))
                    } else {
                        None
                    };
                }
                self.windows = windows;
            }
            Err(error) => self.error = Some(format!("窗口刷新失败：{error:#}")),
        }
    }
    fn register_hotkeys(&mut self) {
        self.hotkeys = None;
        if self.config_error.is_none() {
            match Hotkeys::new(&self.config.game) {
                Ok(keys) => self.hotkeys = Some(keys),
                Err(error) => self.error = Some(format!("{error:#}")),
            }
        }
    }
    fn request(&mut self, task: Task) {
        self.error = None;
        self.message = format!("正在启动{}…", task.name());
        if matches!(
            task,
            Task::Fishing { .. } | Task::Listen { .. } | Task::Record { .. }
        ) {
            self.source = "正在打开音频".into();
            self.fallback.clear();
            self.level = -120.0;
        }
        self.manager.request(task);
    }
    fn start_fishing(&mut self) -> Result<()> {
        anyhow::ensure!(self.config_error.is_none(), "请先修复或恢复配置文件");
        let config = self.store.load()?;
        let window = self.require_window()?;
        self.config = config.clone();
        self.manual_paused = false;
        self.stats = Stats::default();
        self.graph.clear();
        self.preview = None;
        self.calibration = None;
        self.request(Task::Fishing {
            config,
            window,
            store: self.store.clone(),
        });
        Ok(())
    }
    fn save(&mut self, change: impl FnOnce(&mut Config)) -> Result<()> {
        self.config = self.store.update(change)?;
        self.config_error = None;
        self.message = "设置已保存，下次开始时生效".into();
        self.log(self.message.clone());
        Ok(())
    }
    fn button(&mut self, ui: &mut egui::Ui, label: &str) -> bool {
        let response = ui.button(label);
        self.controls.push((label.into(), response.rect));
        response.clicked()
    }
    fn handle(&mut self, event: Event, context: &egui::Context) {
        match event {
            Event::Message(message) => {
                self.message = message.clone();
                self.log(message);
            }
            Event::Error(error) => {
                self.error = Some(error.clone());
                self.log(format!("错误：{error}"));
            }
            Event::Source { name, fallback } => {
                self.source = name;
                self.fallback = fallback;
                self.log(format!("实际音频来源：{} {}", self.source, self.fallback));
            }
            Event::Snapshot {
                state,
                stats,
                manual_paused,
            } => {
                self.state = state;
                self.stats = stats;
                self.manual_paused = manual_paused;
            }
            Event::Scores { peaks, level } => {
                self.level = level;
                self.graph.push_back(peaks);
                if self.graph.len() > 400 {
                    self.graph.pop_front();
                }
            }
            Event::Preview { image, icon } => {
                self.preview = Some(texture(context, "bobber-preview", &image));
                self.preview_icon = icon;
            }
            Event::Calibrated(calibration) => {
                self.calibration = Some(calibration);
                if self.manager.is_fishing() {
                    self.config.vision.icon_size_px = calibration.icon.size;
                    self.config.vision.icon_native_size = calibration.native_size;
                    self.config.vision.icon_threshold = calibration.threshold();
                }
                self.message = format!(
                    "校准：{} 像素，得分 {:.2}，尺度区分 {:.2}",
                    calibration.icon.size, calibration.icon.score, calibration.prominence
                );
                self.log(self.message.clone());
            }
            Event::Recorded { path, report } => {
                self.wav_path = path.display().to_string();
                self.apply_report(report);
            }
            Event::Verified(report) => self.apply_report(report),
            Event::Screenshot(frame) => {
                self.screenshot = Some(texture(context, "water-region", &frame));
                self.region_open = true;
                self.selection = Some(Region::from_config(&self.config.vision));
            }
            Event::Devices(devices) => {
                self.devices = devices;
                self.message = format!("已找到 {} 个输出设备", self.devices.len());
            }
            Event::Doctor(report) => {
                let message = serde_json::to_string_pretty(&report).unwrap_or_default();
                self.log(message);
                self.message = "诊断完成，结果已写入下方日志".into();
            }
            Event::Cancelled(name) => {
                self.message = if let Some(next) = self.manager.active_name() {
                    format!("{name}已取消，正在启动{next}…")
                } else {
                    format!("已取消{name}，可重新开始")
                };
                self.log(self.message.clone());
            }
            Event::Done => {}
        }
    }
    fn apply_report(&mut self, report: Report) {
        self.message = if report.bites.is_empty() {
            "没有检测到足够清晰的咬钩声音，保留当前阈值".into()
        } else {
            format!(
                "检测到 {} 次清晰咬钩声音，建议阈值 {:.2}；点击保存阈值后生效",
                report.bites.len(),
                report.threshold
            )
        };
        self.log(self.message.clone());
        self.report = Some(report);
    }

    fn fishing_page(&mut self, ui: &mut egui::Ui) {
        ui.heading("钓鱼");
        ui.horizontal_wrapped(|ui| {
            let selected = self
                .selected_window()
                .map(|w| w.label())
                .unwrap_or_else(|| "请选择游戏客户端".into());
            egui::ComboBox::from_id_salt("game-window")
                .width(510.0f32.min(ui.available_width()))
                .selected_text(selected)
                .show_ui(ui, |ui| {
                    for window in &self.windows {
                        ui.selectable_value(
                            &mut self.selected,
                            Some((window.handle, window.pid)),
                            window.label(),
                        );
                    }
                });
            if self.button(ui, "刷新客户端") {
                self.refresh_windows();
            }
        });
        ui.horizontal_wrapped(|ui| {
            let enabled = self.config_error.is_none() && !self.closing;
            ui.add_enabled_ui(enabled, |ui| {
                if self.button(ui, "开始钓鱼") {
                    let result = self.start_fishing();
                    self.feedback(result);
                }
            });
            ui.add_enabled_ui(self.manager.is_fishing(), |ui| {
                if self.button(
                    ui,
                    if self.manual_paused {
                        "继续钓鱼"
                    } else {
                        "暂停钓鱼"
                    },
                ) {
                    self.manager.toggle_pause();
                }
            });
            if self.button(ui, "停止") {
                self.manager.stop();
            }
            ui.label(format!(
                "{} 暂停 / 继续 · {} 停止",
                self.config.game.pause_hotkey.to_uppercase(),
                self.config.game.stop_hotkey.to_uppercase()
            ));
        });
        ui.label(
            "游戏内开启自动拾取，绑定“与目标互动”，并执行 /console SoftTargetIconGameObject 1。",
        );
        ui.label("齿轮不存在就抛竿，存在就等待；听到咬钩声音立即按拾取键起杆。");
        ui.small("默认 30 秒没有咬钩声音就重新抛竿，即使金色齿轮仍在。");
        ui.small("切出或最小化游戏会自动暂停，恢复后先检查齿轮。");
        ui.separator();
        ui.horizontal_wrapped(|ui| {
            for (label, value) in [
                ("抛竿", self.stats.casts),
                ("估计收杆", self.stats.catches),
                ("听到咬钩", self.stats.bites),
                ("未成功", self.stats.misses),
            ] {
                egui::Frame::group(ui.style()).show(ui, |ui| {
                    ui.set_min_width(105.0);
                    ui.label(label);
                    ui.label(
                        RichText::new(value.to_string())
                            .size(27.0)
                            .color(Color32::from_rgb(246, 194, 93)),
                    );
                });
            }
            ui.label(format!(
                "会话 {:02}:{:02}:{:02}",
                self.stats.elapsed_s as u64 / 3600,
                self.stats.elapsed_s as u64 / 60 % 60,
                self.stats.elapsed_s as u64 % 60
            ));
        });
        ui.label(format!("当前步骤：{}", self.state.label()));
        self.audio_status(ui);
        if let Some(preview) = &self.preview {
            let size = fit(preview.size_vec2(), ui.available_width().min(640.0), 240.0);
            let response = ui.add(egui::Image::new(preview).fit_to_exact_size(size));
            if let Some(icon) = self.preview_icon {
                let scale = size.x / preview.size_vec2().x;
                let center =
                    response.rect.min + Vec2::new(icon.x as f32 * scale, icon.y as f32 * scale);
                ui.painter().rect_stroke(
                    Rect::from_center_size(center, Vec2::splat(icon.size as f32 * scale)),
                    0.0,
                    Stroke::new(2.0f32, Color32::YELLOW),
                    egui::StrokeKind::Inside,
                );
            }
            ui.small("找到浮漂后，请保持游戏镜头和缩放不变。");
        }
        self.score_graph(ui);
        ui.small("收杆计数依据图标连续消失，表示估计值。实际客户端效果可通过校准和录音验证调整。");
        self.log_panel(ui);
    }

    fn audio_status(&self, ui: &mut egui::Ui) {
        ui.label(format!("实际音频来源：{}", self.source));
        if !self.fallback.is_empty() {
            ui.colored_label(Color32::from_rgb(245, 194, 105), &self.fallback);
        }
        let progress = ((self.level + 60.0) / 60.0).clamp(0.0, 1.0);
        ui.add(
            egui::ProgressBar::new(progress)
                .text(format!("音量 {:.1} dBFS", self.level))
                .desired_width(ui.available_width()),
        );
    }

    fn score_graph(&self, ui: &mut egui::Ui) {
        ui.label("最近的咬钩声音得分");
        let (rect, _) =
            ui.allocate_exact_size(Vec2::new(ui.available_width(), 120.0), Sense::hover());
        ui.painter()
            .rect_filled(rect, 5.0, Color32::from_rgb(20, 27, 35));
        let y = rect.bottom() - self.config.audio.bite_threshold * rect.height();
        ui.painter().line_segment(
            [Pos2::new(rect.left(), y), Pos2::new(rect.right(), y)],
            Stroke::new(1.0f32, Color32::LIGHT_GRAY),
        );
        ui.painter().text(
            Pos2::new(rect.left() + 7.0, y - 3.0),
            egui::Align2::LEFT_BOTTOM,
            format!("阈值 {:.2}", self.config.audio.bite_threshold),
            FontId::proportional(13.0),
            Color32::LIGHT_GRAY,
        );
        let colors = [
            Color32::from_rgb(250, 193, 84),
            Color32::from_rgb(89, 196, 230),
            Color32::from_rgb(118, 207, 146),
            Color32::from_rgb(219, 148, 228),
        ];
        for (clip, &color) in colors.iter().enumerate() {
            let points: Vec<_> = self
                .graph
                .iter()
                .enumerate()
                .map(|(i, scores)| {
                    Pos2::new(
                        rect.left()
                            + i as f32 / self.graph.len().max(2).saturating_sub(1) as f32
                                * rect.width(),
                        rect.bottom()
                            - scores.get(clip).copied().unwrap_or(-1.0).clamp(0.0, 1.0)
                                * rect.height(),
                    )
                })
                .collect();
            if points.len() > 1 {
                ui.painter()
                    .add(egui::Shape::line(points, Stroke::new(1.5f32, color)));
            }
        }
        ui.horizontal_wrapped(|ui| {
            for (label, color) in ["咬钩音 1", "咬钩音 2", "咬钩音 3", "咬钩音"]
                .into_iter()
                .zip(colors)
            {
                ui.colored_label(color, label);
            }
        });
    }

    fn log_panel(&self, ui: &mut egui::Ui) {
        ui.collapsing("运行日志", |ui| {
            egui::ScrollArea::vertical()
                .id_salt("log-panel")
                .max_height(220.0)
                .stick_to_bottom(true)
                .show(ui, |ui| {
                    for line in self.logs.iter().skip(self.logs.len().saturating_sub(300)) {
                        ui.label(line);
                    }
                });
        });
    }

    fn settings_page(&mut self, ui: &mut egui::Ui) {
        ui.heading("设置");
        ui.label(format!("配置文件：{}", self.store.path.display()));
        ui.label(
            "保存后下一次开始生效。按键支持字母、数字、F1–F12、space、enter、tab 和常用标点。",
        );
        egui::Grid::new("game-settings")
            .num_columns(2)
            .spacing([24.0, 10.0])
            .show(ui, |ui| {
                for (label, key) in [
                    ("抛竿键", &mut self.config.game.cast_key),
                    ("拾取 / 起杆键", &mut self.config.game.interact_key),
                    ("补饵宏键（可留空）", &mut self.config.game.lure_key),
                    ("退出宏键（可留空）", &mut self.config.game.quit_key),
                    ("暂停 / 继续快捷键", &mut self.config.game.pause_hotkey),
                    ("停止快捷键", &mut self.config.game.stop_hotkey),
                ] {
                    ui.label(label);
                    ui.add(egui::TextEdit::singleline(key).desired_width(130.0));
                    ui.end_row();
                }
                ui.label("会话时限（分钟，0 不限）");
                ui.add(
                    egui::DragValue::new(&mut self.config.game.session_minutes).range(0..=100000),
                );
                ui.end_row();
                ui.label("补饵周期（分钟）");
                ui.add(
                    egui::DragValue::new(&mut self.config.game.lure_minutes)
                        .range(0.1..=1440.0)
                        .speed(0.1),
                );
                ui.end_row();
                ui.label("自定义窗口标题（可留空）");
                ui.add(
                    egui::TextEdit::singleline(&mut self.config.game.window_title)
                        .desired_width(310.0),
                );
                ui.end_row();
            });
        if self.button(ui, "保存游戏设置") {
            let game = self.config.game.clone();
            let result = self.save(|cfg| cfg.game = game);
            self.feedback(result);
            self.register_hotkeys();
            self.refresh_windows();
        }
        ui.separator();
        ui.heading("金色齿轮识别");
        ui.label("齿轮存在时持续监听，声音命中后立即起杆。识别范围与声音阈值在诊断页调整。");
        ui.add(
            egui::Slider::new(&mut self.config.vision.icon_threshold, 0.5..=0.99).text("图标阈值"),
        );
        if self.button(ui, "保存识别设置") {
            let threshold = self.config.vision.icon_threshold;
            let result = self.save(|cfg| {
                cfg.vision.icon_threshold = threshold;
            });
            self.feedback(result);
        }
        ui.separator();
        ui.collapsing("等待与可选行为", |ui| {
            egui::Grid::new("run-settings")
                .num_columns(2)
                .show(ui, |ui| {
                    for (label, value) in [
                        ("抛竿后齿轮等待（秒）", &mut self.config.run.icon_timeout_s),
                        ("无咬钩声音重抛（秒）", &mut self.config.run.cast_timeout_s),
                        ("补饵等待（秒）", &mut self.config.run.lure_cast_s),
                        ("抛竿间隔下限（秒）", &mut self.config.run.pause_min_s),
                        ("抛竿间隔上限（秒）", &mut self.config.run.pause_max_s),
                    ] {
                        ui.label(label);
                        ui.add(egui::DragValue::new(value).range(0.0..=600.0).speed(0.1));
                        ui.end_row();
                    }
                    ui.label("按键时长（毫秒）");
                    ui.add(egui::DragValue::new(&mut self.config.run.key_hold_ms).range(10..=500));
                    ui.end_row();
                    for (label, value) in [
                        (
                            "休息频率下限（分钟）",
                            &mut self.config.behavior.break_every_min_minutes,
                        ),
                        (
                            "休息频率上限（分钟，0 关闭）",
                            &mut self.config.behavior.break_every_max_minutes,
                        ),
                        (
                            "休息时长下限（分钟）",
                            &mut self.config.behavior.break_min_minutes,
                        ),
                        (
                            "休息时长上限（分钟）",
                            &mut self.config.behavior.break_max_minutes,
                        ),
                    ] {
                        ui.label(label);
                        ui.add(egui::DragValue::new(value).range(0.0..=1440.0).speed(0.1));
                        ui.end_row();
                    }
                    ui.label("按键时长抖动（毫秒）");
                    ui.add(
                        egui::DragValue::new(&mut self.config.behavior.key_hold_jitter_ms)
                            .range(0..=490),
                    );
                    ui.end_row();
                });
            ui.add(
                egui::Slider::new(&mut self.config.behavior.jump_chance, 0.0..=1.0)
                    .text("抛竿前跳跃概率"),
            );
            ui.horizontal(|ui| {
                ui.label("跳跃键");
                ui.add(
                    egui::TextEdit::singleline(&mut self.config.behavior.jump_key)
                        .desired_width(130.0),
                );
            });
            if self.button(ui, "保存等待与可选行为") {
                let run = self.config.run.clone();
                let behavior = self.config.behavior.clone();
                let result = self.save(|cfg| {
                    cfg.run = run;
                    cfg.behavior = behavior;
                });
                self.feedback(result);
            }
        });
        ui.separator();
        ui.horizontal_wrapped(|ui| {
            ui.label("界面字体缩放");
            if ui
                .add(egui::Slider::new(&mut self.font_scale, 1.0..=1.5).step_by(0.05))
                .changed()
            {
                ui.ctx().set_zoom_factor(self.font_scale);
            }
        });
        if self.button(ui, "备份并恢复默认配置") {
            let result = self.store.recover().and_then(|backup| {
                self.config = self.store.load()?;
                self.config_error = None;
                self.message = format!("已恢复默认配置，原配置备份：{}", backup.display());
                self.log(self.message.clone());
                Ok(())
            });
            self.feedback(result);
            self.register_hotkeys();
        }
    }

    fn diagnostics_page(&mut self, ui: &mut egui::Ui) {
        ui.heading("校准与诊断");
        ui.label("先启用交互图标和自动拾取，在游戏里确认键位与宏，再校准自己的浮漂。");
        for (label, command) in [
            ("启用金色齿轮", "/console SoftTargetIconGameObject 1"),
            ("补饵宏示例", "/use 闪光的小珠\n/use 16"),
            ("退出宏示例", "/quit"),
        ] {
            ui.horizontal_wrapped(|ui| {
                ui.label(label);
                ui.monospace(command);
                if self.button(ui, "复制") {
                    ui.ctx().copy_text(command.into());
                }
            });
        }
        ui.separator();
        ui.heading("声音来源");
        ui.horizontal_wrapped(|ui| {
            ui.radio_value(
                &mut self.config.audio.capture,
                "game".into(),
                "选中游戏进程",
            );
            ui.radio_value(
                &mut self.config.audio.capture,
                "device".into(),
                "系统回环设备",
            );
        });
        let device_name = self
            .devices
            .iter()
            .find(|d| d.id == self.config.audio.device)
            .map(|d| d.name.clone())
            .unwrap_or_else(|| {
                if self.config.audio.device.is_empty() {
                    "默认输出设备".into()
                } else {
                    self.config.audio.device.clone()
                }
            });
        egui::ComboBox::from_id_salt("audio-device")
            .width(520.0)
            .selected_text(device_name)
            .show_ui(ui, |ui| {
                ui.selectable_value(&mut self.config.audio.device, String::new(), "默认输出设备");
                for device in &self.devices {
                    ui.selectable_value(
                        &mut self.config.audio.device,
                        device.id.clone(),
                        &device.name,
                    );
                }
            });
        ui.checkbox(
            &mut self.config.audio.fallback_to_device,
            "游戏进程音频不可用时，回退到系统回环并显示原因",
        );
        ui.horizontal_wrapped(|ui| {
            if self.button(ui, "刷新设备") {
                self.request(Task::Devices);
            }
            if self.button(ui, "保存并试听") {
                let audio = self.config.audio.clone();
                let result = self.save(|cfg| {
                    cfg.audio.capture = audio.capture;
                    cfg.audio.device = audio.device;
                    cfg.audio.fallback_to_device = audio.fallback_to_device;
                });
                if result.is_ok() {
                    self.graph.clear();
                    self.request(Task::Listen {
                        config: self.config.clone(),
                        window: self.selected_window(),
                    });
                }
                self.feedback(result);
            }
            if self.button(ui, "停止试听 / 取消任务") {
                self.manager.stop();
            }
        });
        self.audio_status(ui);
        self.score_graph(ui);
        ui.add(
            egui::Slider::new(&mut self.config.audio.bite_threshold, 0.1..=0.99)
                .text("咬钩声音触发阈值"),
        );
        ui.horizontal_wrapped(|ui| {
            if self.button(ui, "保存阈值") {
                let threshold = self.config.audio.bite_threshold;
                let result = self.save(|cfg| cfg.audio.bite_threshold = threshold);
                self.feedback(result);
            }
            if let Some(report) = &self.report {
                let suggestion = report.threshold;
                if ui
                    .add_enabled(
                        !report.bites.is_empty(),
                        egui::Button::new(format!("填入建议 {:.2}", suggestion)),
                    )
                    .clicked()
                {
                    self.config.audio.bite_threshold = suggestion;
                }
            }
        });
        ui.horizontal_wrapped(|ui| {
            ui.label("录音时长（秒）");
            ui.add(egui::DragValue::new(&mut self.recording_seconds).range(5..=600));
            if self.button(ui, "录音验证") {
                let path = self
                    .paths
                    .cache
                    .join(format!("recording-{}.wav", timestamp()));
                self.wav_path = path.display().to_string();
                self.request(Task::Record {
                    config: self.config.clone(),
                    window: self.selected_window(),
                    seconds: self.recording_seconds,
                    path,
                });
            }
        });
        ui.label("录音期间在游戏里手动钓到一次咬钩。分析阶段仍可取消，原始 WAV 保存在缓存目录。");
        ui.horizontal_wrapped(|ui| {
            ui.add(
                egui::TextEdit::singleline(&mut self.wav_path)
                    .desired_width(520.0)
                    .hint_text("已有 WAV 文件路径"),
            );
            if self.button(ui, "分析 WAV") {
                self.request(Task::Verify {
                    path: PathBuf::from(self.wav_path.trim()),
                });
            }
        });
        if let Some(report) = &self.report {
            ui.label(format!(
                "录音 {:.1} 秒，清晰咬钩声音 {} 次，背景最高得分 {:.2}",
                report.duration_s,
                report.bites.len(),
                report.floor
            ));
            for peak in &report.peaks {
                ui.label(format!(
                    "{}：最高 {:.3}，约 {:.2} 秒",
                    peak.clip,
                    peak.score,
                    peak.onset_s.max(0.0)
                ));
            }
        }
        ui.separator();
        ui.heading("图标与水面范围");
        ui.horizontal_wrapped(|ui| {
            if self.button(ui, "校准金色齿轮") {
                let result = self.require_window().map(|window| {
                    self.request(Task::Calibrate {
                        config: self.config.clone(),
                        window,
                    });
                });
                self.feedback(result);
            }
            if self.button(ui, "保存校准") {
                let result = self
                    .calibration
                    .filter(|cal| cal.valid())
                    .context("请先完成有效校准")
                    .and_then(|cal| {
                        self.save(|cfg| {
                            cfg.vision.icon_size_px = cal.icon.size;
                            cfg.vision.icon_native_size = cal.native_size;
                            cfg.vision.icon_threshold = cal.threshold();
                        })
                    });
                self.feedback(result);
            }
            if self.button(ui, "截图并框选水面") {
                let result = self
                    .require_window()
                    .map(|window| self.request(Task::Screenshot { window }));
                self.feedback(result);
            }
            if self.button(ui, "恢复全窗口") {
                let result = self.save(|cfg| {
                    cfg.vision.search_left = 0.0;
                    cfg.vision.search_top = 0.0;
                    cfg.vision.search_right = 1.0;
                    cfg.vision.search_bottom = 1.0;
                });
                self.feedback(result);
            }
        });
        if let Some(cal) = self.calibration {
            ui.label(format!(
                "校准 {} 像素 · 得分 {:.3} · 尺度区分 {:.3}",
                cal.icon.size, cal.icon.score, cal.prominence
            ));
        }
        ui.label(format!(
            "当前范围：({:.2}, {:.2}) → ({:.2}, {:.2})",
            self.config.vision.search_left,
            self.config.vision.search_top,
            self.config.vision.search_right,
            self.config.vision.search_bottom
        ));
        ui.label(
            "校准时切回游戏并手动抛竿，最多观察 30 秒。自动钓鱼也会在图标尺寸不匹配时重新校准。",
        );
        ui.separator();
        ui.horizontal_wrapped(|ui| {
            if self.button(ui, "环境诊断") {
                self.request(Task::Doctor {
                    config: self.config.clone(),
                    window: self.selected_window(),
                });
            }
            if self.button(ui, "导出日志") {
                let path = self.paths.logs.join(format!("export-{}.log", timestamp()));
                let result = fs::write(
                    &path,
                    self.logs.iter().cloned().collect::<Vec<_>>().join("\n"),
                )
                .map_err(anyhow::Error::from);
                if result.is_ok() {
                    self.message = format!("日志已导出：{}", path.display());
                }
                self.feedback(result);
            }
        });
        self.log_panel(ui);
    }

    fn region_window(&mut self, context: &egui::Context) {
        if !self.region_open {
            return;
        }
        let mut open = self.region_open;
        let mut save = false;
        egui::Window::new("框选自己的水面区域")
            .open(&mut open)
            .resizable(true)
            .default_width(760.0)
            .show(context, |ui| {
                ui.label("在画面上拖动鼠标，圈出自己的浮漂落点。保存的范围会跟随游戏窗口缩放。");
                if let Some(image) = &self.screenshot {
                    let size = fit(image.size_vec2(), ui.available_width(), 450.0);
                    let response = ui.add(
                        egui::Image::new(image)
                            .fit_to_exact_size(size)
                            .sense(Sense::drag()),
                    );
                    if response.drag_started() {
                        self.drag_start = response.interact_pointer_pos();
                    }
                    if response.dragged() {
                        if let (Some(start), Some(end)) =
                            (self.drag_start, response.interact_pointer_pos())
                        {
                            let rect = Rect::from_two_pos(start, end).intersect(response.rect);
                            self.selection = Some(Region {
                                left: (rect.left() - response.rect.left()) / response.rect.width(),
                                top: (rect.top() - response.rect.top()) / response.rect.height(),
                                right: (rect.right() - response.rect.left())
                                    / response.rect.width(),
                                bottom: (rect.bottom() - response.rect.top())
                                    / response.rect.height(),
                            });
                        }
                    }
                    if let Some(selection) = self.selection {
                        let a = response.rect.min
                            + Vec2::new(selection.left * size.x, selection.top * size.y);
                        let b = response.rect.min
                            + Vec2::new(selection.right * size.x, selection.bottom * size.y);
                        ui.painter().rect_stroke(
                            Rect::from_two_pos(a, b),
                            0.0,
                            Stroke::new(2.0f32, Color32::YELLOW),
                            egui::StrokeKind::Inside,
                        );
                    }
                }
                if ui.button("保存水面范围").clicked() {
                    save = true;
                }
            });
        self.region_open = open;
        if save {
            let result = self
                .selection
                .context("请拖动鼠标框选有效区域")
                .and_then(|r| {
                    self.save(|cfg| {
                        cfg.vision.search_left = r.left;
                        cfg.vision.search_top = r.top;
                        cfg.vision.search_right = r.right;
                        cfg.vision.search_bottom = r.bottom;
                    })
                });
            if result.is_ok() {
                self.region_open = false;
            }
            self.feedback(result);
        }
    }
}

impl eframe::App for App {
    fn update(&mut self, context: &egui::Context, _frame: &mut eframe::Frame) {
        self.controls.clear();
        let was_busy = self.manager.busy();
        for event in self.manager.poll() {
            self.handle(event, context);
        }
        if let Some(hotkeys) = &self.hotkeys {
            for hotkey in hotkeys.poll() {
                match hotkey {
                    Hotkey::Pause => self.manager.toggle_pause(),
                    Hotkey::Stop => self.manager.stop(),
                }
            }
        }
        if context.input(|i| i.viewport().close_requested()) && self.manager.busy() {
            context.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            self.closing = true;
            self.manager.stop();
        }
        if self.closing && !self.manager.busy() {
            context.send_viewport_cmd(egui::ViewportCommand::Close);
        }
        if !self.manager.busy() {
            self.state = State::Stopped;
            self.manual_paused = false;
            if was_busy {
                self.level = -120.0;
                self.source = format!("已关闭 · {}", self.source);
            }
        }
        if let Some(mut smoke) = self.smoke.take() {
            smoke.before_frame(self, context);
            self.smoke = Some(smoke);
        }
        egui::TopBottomPanel::top("header").show(context, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.label(
                    RichText::new("Forever 钓鱼助手")
                        .size(23.0)
                        .color(Color32::from_rgb(245, 195, 99)),
                );
                ui.small(format!("Rust {}", crate::VERSION));
                for (page, label) in ["钓鱼", "设置", "校准与诊断"].into_iter().enumerate()
                {
                    ui.selectable_value(&mut self.page, page, label);
                }
            });
            if self.manager.cleaning() {
                ui.horizontal_wrapped(|ui| {
                    ui.spinner();
                    ui.label(if let Some(next) = self.manager.pending_name() {
                        format!("正在关闭旧设备，完成后自动开始{next}")
                    } else {
                        "正在取消任务并关闭设备…".into()
                    });
                    if ui.button("取消后续操作").clicked() {
                        self.manager.stop();
                    }
                });
            } else if let Some(name) = self.manager.active_name() {
                ui.horizontal_wrapped(|ui| {
                    ui.label(format!("正在{name} · {}", self.message));
                    if ui.button("取消任务").clicked() {
                        self.manager.stop();
                    }
                });
            } else {
                ui.label(&self.message);
            }
            if let Some(error) = &self.config_error {
                ui.colored_label(Color32::LIGHT_RED, error);
            }
            if let Some(error) = &self.error {
                ui.colored_label(Color32::LIGHT_RED, error);
            }
        });
        egui::CentralPanel::default().show(context, |ui| {
            egui::ScrollArea::vertical()
                .id_salt(("page-scroll", self.page))
                .auto_shrink([false, false])
                .animated(self.smoke.is_none())
                .show(ui, |ui| {
                    let top = ui.cursor().min;
                    match self.page {
                        0 => self.fishing_page(ui),
                        1 => self.settings_page(ui),
                        _ => self.diagnostics_page(ui),
                    }
                    if self.smoke.is_some() {
                        if self.smoke_scroll_bottom {
                            ui.scroll_to_cursor(Some(egui::Align::BOTTOM));
                        } else {
                            ui.scroll_to_rect(
                                Rect::from_min_size(top, Vec2::ZERO),
                                Some(egui::Align::TOP),
                            );
                        }
                    }
                });
        });
        self.region_window(context);
        if let Some(mut smoke) = self.smoke.take() {
            smoke.after_frame(self, context);
            self.smoke = Some(smoke);
        }
        context.request_repaint_after(Duration::from_millis(40));
    }
}

fn timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn texture(context: &egui::Context, name: &str, image: &Frame) -> TextureHandle {
    context.load_texture(
        name,
        egui::ColorImage::from_rgb(
            [image.width() as usize, image.height() as usize],
            image.as_raw(),
        ),
        egui::TextureOptions::LINEAR,
    )
}
fn fit(size: Vec2, max_width: f32, max_height: f32) -> Vec2 {
    size * (max_width / size.x).min(max_height / size.y).min(1.0)
}
