use crate::{
    audio::{self, Cancel, Detector, Report},
    config::{Config, Store},
    engine::{Action, Engine, Observation, State, Stats},
    platform::{self, Window},
    vision::{Calibration, Frame, IconMatch, Matcher, Motion, Region},
};
use anyhow::{Context, Result};
use std::{
    collections::VecDeque,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, Sender},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

#[derive(Clone)]
pub enum Task {
    Fishing {
        config: Config,
        window: Window,
        store: Store,
    },
    Listen {
        config: Config,
        window: Option<Window>,
    },
    Record {
        config: Config,
        window: Option<Window>,
        seconds: u32,
        path: PathBuf,
    },
    Verify {
        path: PathBuf,
    },
    Calibrate {
        config: Config,
        window: Window,
    },
    Screenshot {
        window: Window,
    },
    Devices,
    Doctor {
        config: Config,
        window: Option<Window>,
    },
}

impl Task {
    pub fn name(&self) -> &'static str {
        match self {
            Self::Fishing { .. } => "钓鱼",
            Self::Listen { .. } => "试听",
            Self::Record { .. } => "录音验证",
            Self::Verify { .. } => "WAV 分析",
            Self::Calibrate { .. } => "图标校准",
            Self::Screenshot { .. } => "水面截图",
            Self::Devices => "刷新音频设备",
            Self::Doctor { .. } => "环境诊断",
        }
    }
    pub fn fishing(&self) -> bool {
        matches!(self, Self::Fishing { .. })
    }
}

pub enum Event {
    Message(String),
    Error(String),
    Source {
        name: String,
        fallback: String,
    },
    Snapshot {
        state: State,
        stats: Stats,
        manual_paused: bool,
    },
    Scores {
        peaks: Vec<f32>,
        level: f32,
    },
    Preview {
        image: Frame,
        icon: Option<IconMatch>,
    },
    Calibrated(Calibration),
    Recorded {
        path: PathBuf,
        report: Report,
    },
    Verified(Report),
    Screenshot(Frame),
    Devices(Vec<audio::Device>),
    Doctor(serde_json::Value),
    Cancelled(&'static str),
    Done,
}

struct Worker {
    id: u64,
    task: Task,
    cancel: Cancel,
    paused: Arc<AtomicBool>,
    thread: JoinHandle<()>,
}

/// Retire the old worker completely before starting a queued task, including audio Drop.
pub struct Manager {
    sender: Sender<(u64, Event)>,
    receiver: Receiver<(u64, Event)>,
    active: Option<Worker>,
    pending: Option<Task>,
    next_id: u64,
}

impl Default for Manager {
    fn default() -> Self {
        Self::new()
    }
}
impl Manager {
    pub fn new() -> Self {
        let (sender, receiver) = mpsc::channel();
        Self {
            sender,
            receiver,
            active: None,
            pending: None,
            next_id: 1,
        }
    }
    pub fn request(&mut self, task: Task) {
        if let Some(worker) = &self.active {
            worker.cancel.cancel();
            self.pending = Some(task);
        } else {
            self.start(task);
        }
    }
    pub fn stop(&mut self) {
        self.pending = None;
        if let Some(worker) = &self.active {
            worker.cancel.cancel();
        }
    }
    pub fn toggle_pause(&self) {
        if let Some(worker) = &self.active {
            if worker.task.fishing() {
                worker.paused.fetch_xor(true, Ordering::AcqRel);
            }
        }
    }
    pub fn active_name(&self) -> Option<&'static str> {
        self.active.as_ref().map(|w| w.task.name())
    }
    pub fn busy(&self) -> bool {
        self.active.is_some()
    }
    pub fn is_fishing(&self) -> bool {
        self.active
            .as_ref()
            .is_some_and(|w| w.task.fishing() && !w.cancel.is_cancelled())
    }
    pub fn cleaning(&self) -> bool {
        self.active
            .as_ref()
            .is_some_and(|w| w.cancel.is_cancelled())
    }
    pub fn pending_name(&self) -> Option<&'static str> {
        self.pending.as_ref().map(Task::name)
    }
    fn start(&mut self, task: Task) {
        let id = self.next_id;
        self.next_id += 1;
        let cancel = Cancel::default();
        let paused = Arc::new(AtomicBool::new(false));
        let work_cancel = cancel.clone();
        let work_paused = paused.clone();
        let run = task.clone();
        let sender = self.sender.clone();
        let thread = thread::spawn(move || {
            let emit = move |event| {
                let _ = sender.send((id, event));
            };
            if let Err(error) = run_task(run, &work_cancel, work_paused, &emit) {
                if !work_cancel.is_cancelled() {
                    emit(Event::Error(format!("{error:#}")));
                }
            }
            emit(Event::Done);
        });
        self.active = Some(Worker {
            id,
            task,
            cancel,
            paused,
            thread,
        });
    }
    pub fn poll(&mut self) -> Vec<Event> {
        let mut events = Vec::new();
        for (id, event) in self.receiver.try_iter() {
            if self
                .active
                .as_ref()
                .is_some_and(|w| w.id == id && !w.cancel.is_cancelled())
            {
                events.push(event);
            }
        }
        if self.active.as_ref().is_some_and(|w| w.thread.is_finished()) {
            let worker = self.active.take().unwrap();
            if worker.thread.join().is_err() {
                events.push(Event::Error("后台任务异常退出".into()));
            }
            // The terminal result can arrive after the first drain but before join.
            for (id, event) in self.receiver.try_iter() {
                if id == worker.id && !worker.cancel.is_cancelled() {
                    events.push(event);
                }
            }
            if worker.cancel.is_cancelled() {
                events.push(Event::Cancelled(worker.task.name()));
            }
            if let Some(task) = self.pending.take() {
                self.start(task);
            }
        }
        events
    }
}

impl Drop for Manager {
    fn drop(&mut self) {
        self.stop();
        if let Some(worker) = self.active.take() {
            let _ = worker.thread.join();
        }
    }
}

type Emit<'a> = &'a (dyn Fn(Event) + Sync);

struct Stream {
    packets: Arc<Mutex<VecDeque<Vec<f32>>>>,
    failure: Arc<Mutex<Option<String>>>,
    opened: Arc<AtomicBool>,
    cancel: Cancel,
    thread: Option<JoinHandle<()>>,
}

impl Stream {
    fn spawn(
        config: crate::config::Audio,
        pid: Option<u32>,
        parent: Cancel,
        emit: Sender<(u64, Event)>,
        id: u64,
    ) -> Self {
        let packets = Arc::new(Mutex::new(VecDeque::new()));
        let failure = Arc::new(Mutex::new(None));
        let opened = Arc::new(AtomicBool::new(false));
        let cancel = Cancel::default();
        let out = packets.clone();
        let failed = failure.clone();
        let ready = opened.clone();
        let stop = cancel.clone();
        let thread = thread::spawn(move || {
            let send = |event| {
                let _ = emit.send((id, event));
            };
            let mut source: Option<Box<dyn audio::Capture>> = None;
            let mut retries = 0u32;
            let mut stable: Option<Instant> = None;
            while !parent.is_cancelled() && !stop.is_cancelled() {
                let result = if let Some(source) = &mut source {
                    source.read(&stop)
                } else {
                    match platform::open_audio(&config, pid, &stop) {
                        Ok(capture) => {
                            send(Event::Source {
                                name: capture.name().into(),
                                fallback: capture.fallback_reason().into(),
                            });
                            source = Some(capture);
                            ready.store(true, Ordering::Release);
                            continue;
                        }
                        Err(error) => Err(error),
                    }
                };
                match result {
                    Ok(samples) if !samples.is_empty() => {
                        if stable.is_none() {
                            stable = Some(Instant::now());
                        }
                        if stable.is_some_and(|at| at.elapsed() >= Duration::from_secs(5)) {
                            retries = 0;
                        }
                        let mut queue = out.lock().unwrap();
                        if queue.len() >= 8 {
                            queue.pop_front();
                        }
                        queue.push_back(samples);
                    }
                    Ok(_) => {
                        stop.wait(Duration::from_millis(5));
                    }
                    Err(error) => {
                        source.take();
                        out.lock().unwrap().clear();
                        stable = None;
                        if stop.is_cancelled() || parent.is_cancelled() {
                            break;
                        }
                        if retries >= 3 {
                            *failed.lock().unwrap() = Some(format!("音频连续重连失败：{error:#}"));
                            break;
                        }
                        retries += 1;
                        send(Event::Message(format!(
                            "音频中断，正在重连（{retries}/3）：{error:#}"
                        )));
                        if !stop.wait(Duration::from_secs(1)) {
                            break;
                        }
                    }
                }
            }
            drop(source);
        });
        Self {
            packets,
            failure,
            opened,
            cancel,
            thread: Some(thread),
        }
    }
    fn drain(&self) -> Result<Vec<Vec<f32>>> {
        if let Some(failure) = self.failure.lock().unwrap().as_ref() {
            anyhow::bail!("{failure}");
        }
        Ok(self.packets.lock().unwrap().drain(..).collect())
    }
}
impl Drop for Stream {
    fn drop(&mut self) {
        self.cancel.cancel();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn with_stream<T>(
    config: crate::config::Audio,
    pid: Option<u32>,
    cancel: &Cancel,
    emit: Emit<'_>,
    work: impl FnOnce(&Stream, &Receiver<(u64, Event)>) -> Result<T>,
) -> Result<T> {
    let (sender, receiver) = mpsc::channel();
    let stream = Stream::spawn(config, pid, cancel.clone(), sender, 0);
    while !stream.opened.load(Ordering::Acquire) {
        cancel.check()?;
        relay(&receiver, emit);
        stream.drain()?;
        cancel.wait(Duration::from_millis(20));
    }
    relay(&receiver, emit);
    work(&stream, &receiver)
}

fn relay(receiver: &Receiver<(u64, Event)>, emit: Emit<'_>) {
    for (_, event) in receiver.try_iter() {
        emit(event);
    }
}

fn run_task(task: Task, cancel: &Cancel, paused: Arc<AtomicBool>, emit: Emit<'_>) -> Result<()> {
    cancel.check()?;
    emit(Event::Message(format!("正在启动{}…", task.name())));
    match task {
        Task::Fishing {
            config,
            window,
            store,
        } => {
            config.validate()?;
            let pid = window.pid;
            with_stream(
                config.audio.clone(),
                Some(pid),
                cancel,
                emit,
                |stream, receiver| {
                    fish(
                        config, window, store, stream, receiver, cancel, paused, emit,
                    )
                },
            )
        }
        Task::Listen { config, window } => with_stream(
            config.audio.clone(),
            window.map(|w| w.pid),
            cancel,
            emit,
            |stream, receiver| {
                let mut detector =
                    Detector::new(config.audio.bite_threshold, config.audio.refractory_s)?;
                while !cancel.is_cancelled() {
                    for (_, event) in receiver.try_iter() {
                        if matches!(event, Event::Source { .. }) {
                            detector.reset();
                        }
                        emit(event);
                    }
                    for samples in stream.drain()? {
                        let result = detector.feed(&samples, cancel)?;
                        send_scores(&result, emit);
                        for bite in result.bites {
                            emit(Event::Message(format!(
                                "水花 {} 得分 {:.2}",
                                bite.clip, bite.score
                            )));
                        }
                    }
                    cancel.wait(Duration::from_millis(40));
                }
                Ok(())
            },
        ),
        Task::Record {
            config,
            window,
            seconds,
            path,
        } => {
            with_stream(
                config.audio.clone(),
                window.map(|w| w.pid),
                cancel,
                emit,
                |stream, receiver| {
                    let spec = hound::WavSpec {
                        channels: 1,
                        sample_rate: audio::RATE,
                        bits_per_sample: 16,
                        sample_format: hound::SampleFormat::Int,
                    };
                    let mut writer = hound::WavWriter::create(&path, spec)?;
                    let started = Instant::now();
                    let mut last_note = Instant::now();
                    while !cancel.is_cancelled() && started.elapsed().as_secs_f64() < seconds as f64
                    {
                        relay(receiver, emit);
                        for samples in stream.drain()? {
                            for value in samples {
                                writer.write_sample(
                                    (value * 32767.0).clamp(-32768.0, 32767.0) as i16
                                )?;
                            }
                        }
                        if last_note.elapsed() >= Duration::from_secs(1) {
                            emit(Event::Message(format!(
                                "正在录音：{:.0} / {seconds} 秒",
                                started.elapsed().as_secs_f64()
                            )));
                            last_note = Instant::now();
                        }
                        cancel.wait(Duration::from_millis(30));
                    }
                    writer.finalize()?;
                    cancel.check()?;
                    Ok(())
                },
            )?;
            emit(Event::Message("录音结束，正在分批分析；仍可取消".into()));
            let report = audio::analyze_file(&path, cancel)?;
            emit(Event::Recorded { path, report });
            Ok(())
        }
        Task::Verify { path } => {
            let report = audio::analyze_file(&path, cancel)?;
            emit(Event::Verified(report));
            Ok(())
        }
        Task::Calibrate { config, window } => {
            let mut matcher = Matcher::new(config.vision.match_width)?;
            let started = Instant::now();
            let region = Region::from_config(&config.vision);
            while started.elapsed() < Duration::from_secs(30) {
                cancel.check()?;
                let window = platform::refresh(&window)?.context("游戏窗口已关闭")?;
                if platform::focused(&window) && !window.minimized {
                    let frame = platform::capture(&window, Region::default())?;
                    let cal = matcher.calibrate(&frame, region, cancel)?;
                    emit(Event::Preview {
                        image: thumbnail(&frame),
                        icon: Some(scale_icon(cal.icon, frame.width())),
                    });
                    if cal.valid() {
                        emit(Event::Calibrated(cal));
                        return Ok(());
                    }
                }
                cancel.wait(Duration::from_millis(250));
            }
            anyhow::bail!("30 秒内未找到清晰的金色齿轮；请切回游戏、手动抛竿并检查识别范围")
        }
        Task::Screenshot { window } => {
            emit(Event::Message("3 秒后截图，请切回游戏窗口".into()));
            cancel.wait(Duration::from_secs(3));
            cancel.check()?;
            anyhow::ensure!(
                platform::focused(&window),
                "请切回选中的游戏窗口再截图，避免截到遮挡的画面"
            );
            emit(Event::Screenshot(platform::capture(
                &window,
                Region::default(),
            )?));
            Ok(())
        }
        Task::Devices => {
            emit(Event::Devices(platform::devices()?));
            Ok(())
        }
        Task::Doctor { config, window } => {
            config.validate()?;
            cancel.check()?;
            let fingerprint_count = audio::Fingerprint::bundled()?.len();
            let devices = platform::devices()?;
            cancel.check()?;
            let mut result = serde_json::json!({ "version": crate::VERSION, "os": std::env::consts::OS,
                "configuration": "有效", "embedded_icons": crate::assets::ICONS.len(), "fingerprints": fingerprint_count,
                "devices": devices, "window": window, "game_automation": cfg!(windows) });
            if cfg!(windows) && (config.audio.capture == "device" || window.is_some()) {
                let source =
                    platform::open_audio(&config.audio, window.as_ref().map(|w| w.pid), cancel);
                cancel.check()?;
                result["audio"] = match source {
                    Ok(source) => {
                        serde_json::json!({ "name": source.name(), "fallback": source.fallback_reason(), "opened": true })
                    }
                    Err(error) => {
                        serde_json::json!({ "opened": false, "error": format!("{error:#}") })
                    }
                };
            }
            cancel.check()?;
            emit(Event::Doctor(result));
            Ok(())
        }
    }
}

fn send_scores(result: &audio::Feed, emit: Emit<'_>) {
    let mut peaks = vec![-1.0f32; crate::assets::CLIPS.len()];
    for frame in &result.frames {
        for (i, &score) in frame.scores.iter().enumerate() {
            peaks[i] = peaks[i].max(score);
        }
    }
    emit(Event::Scores {
        peaks,
        level: result.level_dbfs,
    });
}

fn thumbnail(frame: &Frame) -> Frame {
    if frame.width() <= 640 {
        frame.clone()
    } else {
        image::imageops::resize(
            frame,
            640,
            ((frame.height() as f32 * 640.0 / frame.width() as f32).round() as u32).max(1),
            image::imageops::FilterType::Triangle,
        )
    }
}
fn scale_icon(icon: IconMatch, width: u32) -> IconMatch {
    let scale = (640.0 / width as f32).min(1.0);
    IconMatch {
        x: (icon.x as f32 * scale).round() as u32,
        y: (icon.y as f32 * scale).round() as u32,
        size: (icon.size as f32 * scale).round().max(2.0) as u32,
        ..icon
    }
}

#[allow(clippy::too_many_arguments)]
fn fish(
    config: Config,
    mut window: Window,
    store: Store,
    stream: &Stream,
    receiver: &Receiver<(u64, Event)>,
    cancel: &Cancel,
    paused: Arc<AtomicBool>,
    emit: Emit<'_>,
) -> Result<()> {
    let mut detector = Detector::new(config.audio.bite_threshold, config.audio.refractory_s)?;
    let mut matcher = Matcher::new(config.vision.match_width)?;
    let mut motion = Motion::new(config.vision.motion_threshold);
    let mut engine = Engine::new(config);
    let start = Instant::now();
    let mut last_vision = f64::NEG_INFINITY;
    let mut last_snapshot = f64::NEG_INFINITY;
    let mut last_sweep = f64::NEG_INFINITY;
    while engine.state != State::Stopped && !cancel.is_cancelled() {
        for (_, event) in receiver.try_iter() {
            if matches!(event, Event::Source { .. }) {
                detector.reset();
                motion.reset();
            }
            emit(event);
        }
        let tick = Instant::now();
        let now = start.elapsed().as_secs_f64();
        let fresh = platform::refresh(&window)?;
        let mut observed = Observation {
            alive: fresh.is_some(),
            focused: false,
            ..Default::default()
        };
        if let Some(fresh) = fresh {
            observed.resized = (window.width, window.height) != (fresh.width, fresh.height);
            window = fresh;
            observed.focused = platform::focused(&window) && !window.minimized;
        }
        engine.manual_paused = paused.load(Ordering::Acquire);
        for samples in stream.drain()? {
            if engine.state == State::Waiting && !engine.manual_paused && observed.focused {
                let result = detector.feed(&samples, cancel)?;
                observed.bite = result.bites.first().cloned().or(observed.bite);
                send_scores(&result, emit);
            }
        }
        if observed.focused
            && !engine.manual_paused
            && engine.wants_vision()
            && now - last_vision >= 0.1
            && !observed.resized
        {
            last_vision = now;
            let frame = platform::capture(&window, Region::default())?;
            let allowed = Region::from_config(&engine.cfg.vision);
            let region = engine
                .icon
                .map(|icon| {
                    Region::around(
                        icon.x,
                        icon.y,
                        frame.width(),
                        frame.height(),
                        (frame.width().max(frame.height()) as f32 * 0.1) as u32,
                    )
                    .intersection(allowed)
                })
                .unwrap_or(allowed);
            let mut found = IconMatch::none(engine.cfg.vision.icon_size_px.max(12));
            if engine.cfg.vision.icon_size_px > 0 {
                found = matcher.find(&frame, engine.cfg.vision.icon_size_px, region, cancel)?;
            }
            if engine.state == State::Casting
                && !found.accepted(engine.cfg.vision.icon_threshold)
                && now - last_sweep >= 1.0
            {
                last_sweep = now;
                let cal = matcher.calibrate(&frame, allowed, cancel)?;
                if cal.valid() {
                    engine.cfg.vision.icon_size_px = cal.icon.size;
                    engine.cfg.vision.icon_native_size = cal.native_size;
                    engine.cfg.vision.icon_threshold = cal.threshold();
                    found = cal.icon;
                    if let Err(error) = store.update(|cfg| {
                        cfg.vision.icon_size_px = cal.icon.size;
                        cfg.vision.icon_native_size = cal.native_size;
                        cfg.vision.icon_threshold = cal.threshold();
                    }) {
                        emit(Event::Message(format!(
                            "校准已用于本次钓鱼，但保存失败：{error:#}"
                        )));
                    }
                    emit(Event::Calibrated(cal));
                }
            }
            observed.icon = Some(found);
            if let Some(icon) = engine.icon {
                if engine.state == State::Waiting && engine.cfg.vision.confirm_bite {
                    let side = icon.size.max(16);
                    let x0 = icon.x.saturating_sub(side * 2);
                    let y0 = icon.y.saturating_sub(side);
                    let x1 = (icon.x + side * 2).min(frame.width());
                    let y1 = (icon.y + side * 4).min(frame.height());
                    if x1 > x0 && y1 > y0 {
                        let crop =
                            image::imageops::crop_imm(&frame, x0, y0, x1 - x0, y1 - y0).to_image();
                        motion.observe(&crop, start.elapsed().as_secs_f64());
                    }
                }
            }
            if found.accepted(engine.cfg.vision.icon_threshold) && engine.state == State::Casting {
                emit(Event::Preview {
                    image: thumbnail(&frame),
                    icon: Some(scale_icon(found, frame.width())),
                });
            }
        }
        observed.visual_confirmed = motion.confirmed(start.elapsed().as_secs_f64());
        // Recheck focus and manual pause after a potentially expensive calibration pass.
        observed.focused &= platform::focused(&window);
        engine.manual_paused = paused.load(Ordering::Acquire);
        let previous = engine.state;
        for action in engine.tick(start.elapsed().as_secs_f64(), observed) {
            match action {
                Action::Press { key, hold_ms } => {
                    if !paused.load(Ordering::Acquire) {
                        platform::press(&window, &key, hold_ms, cancel)?;
                    }
                }
                Action::ResetDetection => {
                    detector.reset();
                    motion.reset();
                    let _ = stream.drain()?;
                }
                Action::Message(message) | Action::Finished(message) => {
                    emit(Event::Message(message))
                }
            }
        }
        let now = start.elapsed().as_secs_f64();
        if engine.state != previous || now - last_snapshot >= 0.2 {
            last_snapshot = now;
            emit(Event::Snapshot {
                state: engine.state,
                stats: engine.stats.clone(),
                manual_paused: engine.manual_paused,
            });
        }
        let interval = Duration::from_secs_f64(engine.cfg.run.tick_interval_s);
        cancel.wait(interval.saturating_sub(tick.elapsed()));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canceled_result_is_discarded_and_queued_task_waits_for_cleanup() {
        let mut manager = Manager::new();
        let cancel = Cancel::default();
        let (started, opened) = mpsc::channel();
        let (close, wait_close) = mpsc::channel();
        let output = manager.sender.clone();
        let child_cancel = cancel.clone();
        let task = Task::Devices;
        let thread = thread::spawn(move || {
            started.send(()).unwrap();
            while !child_cancel.is_cancelled() {
                thread::sleep(Duration::from_millis(1));
            }
            output
                .send((1, Event::Message("旧设备的延迟结果".into())))
                .unwrap();
            wait_close.recv().unwrap();
        });
        manager.active = Some(Worker {
            id: 1,
            task,
            cancel,
            paused: Arc::new(AtomicBool::new(false)),
            thread,
        });
        manager.next_id = 2;
        opened.recv().unwrap();
        manager.request(Task::Devices);
        assert!(manager.cleaning());
        assert_eq!(manager.pending_name(), Some("刷新音频设备"));
        assert!(manager.poll().is_empty());
        assert_eq!(manager.active.as_ref().unwrap().id, 1);
        close.send(()).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while manager.active.as_ref().is_some_and(|w| w.id == 1) {
            assert!(manager
                .poll()
                .iter()
                .all(|e| !matches!(e, Event::Message(m) if m.contains("延迟"))));
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(1));
        }
        assert_eq!(manager.active.as_ref().unwrap().id, 2);
    }

    #[test]
    fn stop_removes_pending_task_and_terminal_result_is_delivered() {
        let mut manager = Manager::new();
        manager.request(Task::Devices);
        manager.pending = Some(Task::Devices);
        manager.stop();
        assert!(manager.pending.is_none());
        let mut canceled = false;
        while manager.busy() {
            for event in manager.poll() {
                if matches!(event, Event::Cancelled("刷新音频设备")) {
                    canceled = true;
                }
            }
            thread::sleep(Duration::from_millis(1));
        }
        assert!(canceled);
        manager.request(Task::Devices);
        let mut completed = false;
        while manager.busy() {
            for event in manager.poll() {
                if matches!(event, Event::Done) {
                    completed = true;
                }
            }
            thread::sleep(Duration::from_millis(1));
        }
        assert!(completed);
    }
}
