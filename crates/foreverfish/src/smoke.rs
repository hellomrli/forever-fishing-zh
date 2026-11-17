use crate::{
    audio::{self, Cancel},
    config::{Config, Store},
    gui::App,
    platform,
    vision::{Matcher, Region},
};
use anyhow::{ensure, Context, Result};
use eframe::egui;
use std::{
    collections::HashSet,
    path::PathBuf,
    time::{Duration, Instant},
};

const CASES: &[(usize, bool, &[&str])] = &[
    (0, false, &["开始钓鱼", "暂停钓鱼", "停止"]),
    (1, false, &[]),
    (1, true, &["备份并恢复默认配置"]),
    (2, false, &["复制"]),
    (2, true, &["环境诊断", "导出日志"]),
];
const SCALES: &[u32] = &[100, 125, 150];

pub struct Smoke {
    path: PathBuf,
    case: usize,
    frames: usize,
    waiting: bool,
    screenshots: Vec<PathBuf>,
    layouts: Vec<serde_json::Value>,
    checks: Option<serde_json::Value>,
    errors: Vec<String>,
    started: Instant,
    finished: bool,
}

impl Smoke {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            case: 0,
            frames: 0,
            waiting: false,
            screenshots: vec![],
            layouts: vec![],
            checks: None,
            errors: vec![],
            started: Instant::now(),
            finished: false,
        }
    }
    pub fn before_frame(&mut self, app: &mut App, context: &egui::Context) {
        if self.finished {
            return;
        }
        let events = context.input(|i| i.events.clone());
        for event in events {
            if let egui::Event::Screenshot {
                user_data, image, ..
            } = event
            {
                let case = user_data
                    .data
                    .as_ref()
                    .and_then(|value| value.downcast_ref::<usize>())
                    .copied();
                if case == Some(self.case) && self.waiting {
                    let (page, bottom, _) = CASES[self.case % CASES.len()];
                    let scale = SCALES[self.case / CASES.len()];
                    let position = if bottom { "bottom" } else { "top" };
                    let path = self
                        .path
                        .parent()
                        .unwrap_or(std::path::Path::new("."))
                        .join(format!(
                            "rust-page-{}-{position}-scale-{scale}.png",
                            page + 1
                        ));
                    let colors: HashSet<_> = image.pixels.iter().step_by(257).collect();
                    if colors.len() < 8 {
                        self.errors
                            .push(format!("界面截图缺少绘制内容：{}", path.display()));
                    }
                    let rgba: Vec<u8> = image
                        .pixels
                        .iter()
                        .flat_map(|color| color.to_array())
                        .collect();
                    let result = image::save_buffer(
                        &path,
                        &rgba,
                        image.size[0] as u32,
                        image.size[1] as u32,
                        image::ColorType::Rgba8,
                    );
                    match result {
                        Ok(()) => self.screenshots.push(path),
                        Err(error) => self.errors.push(format!("保存界面截图失败：{error}")),
                    }
                    self.case += 1;
                    self.frames = 0;
                    self.waiting = false;
                }
            }
        }
        if self.case >= CASES.len() * SCALES.len() {
            match verify_core() {
                Ok(checks) => self.checks = Some(checks),
                Err(error) => self.errors.push(format!("核心自检失败：{error:#}")),
            }
            self.finish(context);
            return;
        }
        if self.started.elapsed() > Duration::from_secs(60) {
            self.errors.push("界面截图自检超过 60 秒".into());
            self.finish(context);
            return;
        }
        let (page, bottom, _) = CASES[self.case % CASES.len()];
        app.page = page;
        app.smoke_scroll_bottom = bottom;
        let zoom = SCALES[self.case / CASES.len()] as f32 / 100.0;
        app.font_scale = zoom;
        context.set_zoom_factor(zoom);
    }
    pub fn after_frame(&mut self, app: &App, context: &egui::Context) {
        if self.finished {
            return;
        }
        self.frames += 1;
        if self.frames >= 3 && !self.waiting {
            if app.config_error.is_some() {
                self.errors.push("自检默认配置无效".into());
            }
            let glyphs = context.fonts(|fonts| {
                fonts.has_glyphs(
                    &egui::FontId::proportional(16.0),
                    "钓鱼设置校准诊断暂停继续停止",
                )
            });
            if !glyphs {
                self.errors.push("内置中文字体缺少必要字形".into());
            }
            let (page, bottom, labels) = CASES[self.case % CASES.len()];
            let scale = SCALES[self.case / CASES.len()];
            let screen = context.screen_rect();
            let mut visible_controls = serde_json::Map::new();
            for &label in labels {
                let visible = app
                    .controls
                    .iter()
                    .any(|(name, rect)| name == label && screen.contains_rect(*rect));
                visible_controls.insert(label.into(), visible.into());
                if !visible {
                    self.errors
                        .push(format!("{scale}% 字体下的 {label} 不可见"));
                }
            }
            self.layouts.push(serde_json::json!({
                "page": (["钓鱼", "设置", "校准与诊断"][page]),
                "font_scale": scale,
                "scroll_position": if bottom { "bottom" } else { "top" },
                "visible_controls": visible_controls,
            }));
            context.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::UserData::new(
                self.case,
            )));
            self.waiting = true;
        }
    }
    fn finish(&mut self, context: &egui::Context) {
        self.finished = true;
        let report = serde_json::json!({ "ok": self.errors.is_empty() && self.screenshots.len() == CASES.len() * SCALES.len(),
            "version": crate::VERSION, "native_rust": true, "window": [900, 640],
            "pages": ["钓鱼", "设置", "校准与诊断"], "font_scales": [100, 125, 150],
            "screenshots": self.screenshots, "layouts": self.layouts, "checks": self.checks, "errors": self.errors,
            "elapsed_s": self.started.elapsed().as_secs_f64() });
        if let Some(parent) = self.path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Err(error) =
            std::fs::write(&self.path, serde_json::to_string_pretty(&report).unwrap())
        {
            eprintln!("无法写入自检报告：{error}");
        }
        context.send_viewport_cmd(egui::ViewportCommand::Close);
    }
}

fn verify_core() -> Result<serde_json::Value> {
    let temporary = tempfile::tempdir()?;
    let store = Store::new(temporary.path().join("中文配置.toml"));
    store.ensure()?;
    ensure!(store.load()? == Config::default(), "默认配置不一致");
    store.update(|cfg| cfg.vision.icon_size_px = 26)?;
    store.update(|cfg| cfg.audio.device = "中文测试扬声器".into())?;
    ensure!(
        store.load()?.vision.icon_size_px == 26,
        "配置更新覆盖了校准结果"
    );
    let text = std::fs::read_to_string(&store.path)?;
    ensure!(
        text.contains("#") && text.contains("中文测试扬声器"),
        "注释或 UTF-8 字段丢失"
    );
    let mut matcher = Matcher::new(1920)?;
    let image = image::load_from_memory(crate::assets::ICONS[0].1)?.into_rgb8();
    let mut scene = image::RgbImage::from_pixel(240, 180, image::Rgb([25, 65, 110]));
    image::imageops::replace(&mut scene, &image, 70, 50);
    let found = matcher.find(&scene, 29, Region::default(), &Cancel::default())?;
    ensure!(found.accepted(0.99), "内置图标匹配失败：{found:?}");
    let (clip, rate) = audio::decode_wav(std::io::Cursor::new(crate::assets::CLIPS[0].1))?;
    let report = audio::analyze_samples(&clip, rate, &Cancel::default())?;
    ensure!(
        report.peaks.iter().any(|p| p.score > 0.999),
        "内置水花分析失败"
    );
    let native = platform::native_smoke().context("Windows 原生接口自检")?;
    Ok(
        serde_json::json!({ "config_utf8_comments": true, "config_latest_update": true,
        "embedded_icons": crate::assets::ICONS.len(), "embedded_clips": crate::assets::CLIPS.len(),
        "vision_score": found.score, "audio_peak": report.peaks.iter().map(|p| p.score).fold(-1.0, f32::max),
        "platform": native }),
    )
}
