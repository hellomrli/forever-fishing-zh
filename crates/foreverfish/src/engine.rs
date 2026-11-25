use crate::{audio::Bite, config::Config, vision::IconMatch};
use rand::{rngs::StdRng, Rng, SeedableRng};
use serde::Serialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub enum State {
    Idle,
    Lure,
    Jump,
    Casting,
    Waiting,
    Looting,
    Resting,
    Paused,
    Quitting,
    Stopped,
}

impl State {
    pub fn label(self) -> &'static str {
        match self {
            Self::Idle => "检查金色齿轮",
            Self::Lure => "正在补饵",
            Self::Jump => "等待落地",
            Self::Casting => "寻找金色齿轮",
            Self::Waiting => "等待咬钩声音",
            Self::Looting => "起杆后继续监听",
            Self::Resting => "休息等待",
            Self::Paused => "已暂停",
            Self::Quitting => "等待退出游戏",
            Self::Stopped => "已停止",
        }
    }
    fn busy(self) -> bool {
        matches!(self, Self::Casting | Self::Waiting | Self::Looting)
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Stats {
    pub casts: u64,
    pub catches: u64,
    pub misses: u64,
    pub bites: u64,
    pub elapsed_s: f64,
}

#[derive(Clone, Debug)]
pub enum Action {
    Press { key: String, hold_ms: u32 },
    ResetDetection,
    Message(String),
    Finished(String),
}

#[derive(Clone, Debug)]
pub struct Observation {
    pub alive: bool,
    pub focused: bool,
    pub resized: bool,
    pub icon: Option<IconMatch>,
    pub bite: Option<Bite>,
}

/// Ignore brief icon flicker; a fresh observation must confirm the sustained absence.
const ICON_LOST_S: f64 = 1.0; // Let the 0.8 s sound fingerprint arrive before recasting.
pub const MEASURE_AFTER_S: f64 = 1.5;

impl Default for Observation {
    fn default() -> Self {
        Self {
            alive: true,
            focused: true,
            resized: false,
            icon: None,
            bite: None,
        }
    }
}

/// Pure, nonblocking state machine: every delay is a deadline checked on the next tick.
/// Capture and key delivery are outside this type and can be replaced in regression tests.
pub struct Engine {
    pub cfg: Config,
    pub state: State,
    pub stats: Stats,
    pub icon: Option<IconMatch>,
    pub manual_paused: bool,
    rng: StdRng,
    started: Option<f64>,
    deadline: f64,
    tick_at: f64,
    cast_at: f64,
    listen_at: f64,
    lure_at: Option<f64>,
    disappeared: Option<f64>,
    misses: usize,
    next_break: Option<f64>,
    quit_tries: u32,
    stopping: bool,
}

impl Engine {
    pub fn new(cfg: Config) -> Self {
        Self::with_seed(cfg, rand::random())
    }
    pub fn with_seed(cfg: Config, seed: u64) -> Self {
        Self {
            cfg,
            state: State::Idle,
            stats: Stats::default(),
            icon: None,
            manual_paused: false,
            rng: StdRng::seed_from_u64(seed),
            started: None,
            deadline: 0.0,
            tick_at: 0.0,
            cast_at: 0.0,
            listen_at: 0.0,
            lure_at: None,
            disappeared: None,
            misses: 0,
            next_break: None,
            quit_tries: 0,
            stopping: false,
        }
    }
    pub fn toggle_pause(&mut self) {
        self.manual_paused = !self.manual_paused;
    }
    pub fn stop(&mut self) {
        self.stopping = true;
    }
    pub fn wants_vision(&self, now: f64) -> bool {
        match self.state {
            State::Casting => {
                now - self.cast_at >= MEASURE_AFTER_S
                    && now < self.listen_at + self.cfg.run.cast_timeout_s
            }
            State::Idle => true,
            State::Waiting | State::Looting => now < self.listen_at + self.cfg.run.cast_timeout_s,
            _ => false,
        }
    }
    /// Capture and key delivery may take time; gameplay deadlines start after delivery.
    pub fn key_pressed(&mut self, now: f64) {
        match self.state {
            State::Casting => {
                self.cast_at = now;
                self.listen_at = now;
            }
            State::Looting => self.listen_at = now,
            State::Lure | State::Jump | State::Quitting => {
                self.deadline += (now - self.tick_at).max(0.0);
            }
            _ => {}
        }
    }
    pub fn key_cancelled(&mut self) -> Vec<Action> {
        let mut actions = vec![Action::ResetDetection];
        if self.state == State::Quitting {
            self.finish("退出按键发送前游戏失去焦点，已停止", &mut actions);
        } else {
            if self.state == State::Casting {
                self.stats.casts = self.stats.casts.saturating_sub(1);
            }
            self.state = State::Paused;
            self.icon = None;
            actions.push(Action::Message(
                "按键发送前游戏失去焦点或已暂停；继续后先检查齿轮".into(),
            ));
        }
        actions
    }

    pub fn tick(&mut self, now: f64, observed: Observation) -> Vec<Action> {
        let mut actions = Vec::new();
        if self.state == State::Stopped {
            return actions;
        }
        self.tick_at = now;
        if self.started.is_none() {
            self.started = Some(now);
            self.schedule_break(now);
        }
        self.stats.elapsed_s = (now - self.started.unwrap()).max(0.0);
        if self.stopping {
            self.finish("用户停止", &mut actions);
            return actions;
        }
        if !observed.alive {
            self.finish(
                if self.state == State::Quitting {
                    "游戏已退出"
                } else {
                    "游戏窗口已关闭"
                },
                &mut actions,
            );
            return actions;
        }
        let time_up = self.cfg.game.session_minutes > 0
            && self.stats.elapsed_s >= self.cfg.game.session_minutes as f64 * 60.0;
        if time_up
            && (!self.state.busy() || self.cfg.game.quit_key.trim().is_empty())
            && self.state != State::Quitting
        {
            if self.cfg.game.quit_key.trim().is_empty() {
                self.finish("会话时间已到", &mut actions);
                return actions;
            }
            if !observed.focused || self.manual_paused {
                self.finish("会话时间已到，游戏不在前台或已暂停", &mut actions);
                return actions;
            }
            self.state = State::Quitting;
            self.quit_tries = 1;
            self.deadline = now + 30.0;
            let key = self.cfg.game.quit_key.clone();
            self.press(key, &mut actions);
            actions.push(Action::Message(
                "会话时间已到，已按退出宏键，等待游戏关闭".into(),
            ));
            return actions;
        }
        if !observed.focused || self.manual_paused {
            if self.state == State::Quitting {
                self.finish("退出时游戏失去焦点，已停止发送按键", &mut actions);
                return actions;
            }
            if self.state != State::Paused {
                self.state = State::Paused;
                actions.push(Action::Message(
                    if self.manual_paused {
                        "手动暂停；继续后先检查齿轮"
                    } else {
                        "游戏未在前台或已最小化，切回后先检查齿轮"
                    }
                    .into(),
                ));
                actions.push(Action::ResetDetection);
            }
            return actions;
        }
        if self.state == State::Paused {
            self.icon = None;
            self.misses = 0;
            self.disappeared = None;
            self.state = State::Idle;
            actions.push(Action::ResetDetection);
            actions.push(Action::Message("已恢复，先检查金色齿轮".into()));
            return actions;
        }
        if observed.resized {
            self.icon = None;
            self.misses = 0;
            self.disappeared = None;
        }
        match self.state {
            State::Idle => {
                let Some(icon) = observed.icon else {
                    return actions; // No screenshot yet: absence has not been established.
                };
                if icon.accepted(self.cfg.vision.icon_threshold) {
                    self.found(now, icon, &mut actions);
                } else if !self.cfg.game.lure_key.trim().is_empty()
                    && self
                        .lure_at
                        .is_none_or(|at| now - at >= self.cfg.game.lure_minutes * 60.0)
                {
                    let key = self.cfg.game.lure_key.clone();
                    self.press(key, &mut actions);
                    self.state = State::Lure;
                    self.deadline = now + self.cfg.run.lure_cast_s;
                    actions.push(Action::Message(format!(
                        "正在补饵，等待 {:.1} 秒",
                        self.cfg.run.lure_cast_s
                    )));
                } else if self.chance(self.cfg.behavior.jump_chance) {
                    let key = self.cfg.behavior.jump_key.clone();
                    self.press(key, &mut actions);
                    self.state = State::Jump;
                    self.deadline = now + self.uniform(1.0, 1.5);
                } else {
                    self.cast(now, &mut actions);
                }
            }
            State::Lure if now >= self.deadline => {
                self.lure_at = Some(now);
                self.state = State::Idle;
                actions.push(Action::Message("补饵等待完成".into()));
            }
            State::Jump if now >= self.deadline => self.state = State::Idle,
            State::Casting => {
                if now - self.cast_at >= MEASURE_AFTER_S {
                    if now >= self.listen_at + self.cfg.run.cast_timeout_s {
                        self.bite_timeout(now, time_up, &mut actions);
                    } else if let Some(icon) = observed.icon {
                        if icon.accepted(self.cfg.vision.icon_threshold) {
                            self.found(now, icon, &mut actions);
                        } else if now - self.cast_at >= self.cfg.run.icon_timeout_s {
                            self.miss(
                                now,
                                "抛竿后未出现金色齿轮，请检查命令、校准和识别范围",
                                &mut actions,
                            );
                        }
                    }
                }
            }
            State::Waiting | State::Looting => {
                if let Some(bite) = observed.bite {
                    self.stats.bites += 1;
                    let key = self.cfg.game.interact_key.clone();
                    self.press(key.clone(), &mut actions);
                    self.state = State::Looting;
                    self.listen_at = now;
                    self.misses = 0;
                    self.disappeared = None;
                    actions.push(Action::Message(format!(
                        "听到咬钩声音 {}（{:.2}），立即按 {key} 起杆；齿轮存在时继续监听",
                        bite.clip, bite.score,
                    )));
                    return actions; // Sound wins over a missing icon in the same tick.
                }
                if now >= self.listen_at + self.cfg.run.cast_timeout_s {
                    self.bite_timeout(now, time_up, &mut actions);
                    return actions; // A capture finishing late must not delay the recast.
                }
                if let Some(icon) = observed.icon {
                    if icon.accepted(self.cfg.vision.icon_threshold) {
                        self.icon = Some(icon);
                        self.misses = 0;
                        self.disappeared = None;
                    } else {
                        self.misses += 1;
                        self.disappeared.get_or_insert(now);
                    }
                    if self.misses >= 3
                        && self.disappeared.is_some_and(|at| now - at >= ICON_LOST_S)
                    {
                        if self.state == State::Looting {
                            self.stats.catches += 1;
                            self.rest(now, &mut actions);
                            actions.push(Action::Message("齿轮持续消失，估计收杆 +1".into()));
                        } else {
                            self.miss(now, "金色齿轮持续消失，准备下一竿", &mut actions);
                        }
                    }
                }
            }
            State::Resting if now >= self.deadline => {
                self.state = State::Idle;
            }
            State::Quitting if now >= self.deadline => {
                if self.quit_tries >= 3 {
                    self.finish("已尝试退出宏 3 次，游戏仍未关闭，请检查宏", &mut actions);
                } else {
                    self.quit_tries += 1;
                    self.deadline = now + 30.0;
                    let key = self.cfg.game.quit_key.clone();
                    self.press(key, &mut actions);
                }
            }
            _ => {}
        }
        actions
    }

    fn bite_timeout(&mut self, now: f64, time_up: bool, actions: &mut Vec<Action>) {
        let message = format!(
            "{:.0} 秒内没有识别到咬钩声音，重新抛竿",
            self.cfg.run.cast_timeout_s
        );
        if time_up {
            self.miss(now, &message, actions);
        } else {
            self.stats.misses += 1;
            actions.push(Action::Message(message));
            self.cast(now, actions);
        }
    }

    fn press(&mut self, key: String, actions: &mut Vec<Action>) {
        let jitter = self.cfg.behavior.key_hold_jitter_ms as f64;
        let hold = (self.cfg.run.key_hold_ms as f64 + self.uniform(-jitter, jitter)).round() as u32;
        actions.push(Action::Press { key, hold_ms: hold });
    }
    fn cast(&mut self, now: f64, actions: &mut Vec<Action>) {
        self.icon = None;
        self.disappeared = None;
        self.misses = 0;
        actions.push(Action::ResetDetection);
        let key = self.cfg.game.cast_key.clone();
        self.press(key, actions);
        self.cast_at = now;
        self.listen_at = now;
        self.stats.casts += 1;
        self.state = State::Casting;
        actions.push(Action::Message("已抛竿，等待浮漂落水后寻找金色齿轮".into()));
    }
    fn found(&mut self, now: f64, icon: IconMatch, actions: &mut Vec<Action>) {
        if self.state != State::Casting {
            self.listen_at = now;
        }
        self.icon = Some(icon);
        self.misses = 0;
        self.disappeared = None;
        self.state = State::Waiting;
        actions.push(Action::ResetDetection);
        actions.push(Action::Message(format!(
            "金色齿轮存在（{} 像素，得分 {:.2}），等待咬钩声音",
            icon.size, icon.score
        )));
    }
    fn miss(&mut self, now: f64, message: &str, actions: &mut Vec<Action>) {
        self.stats.misses += 1;
        self.rest(now, actions);
        actions.push(Action::Message(message.into()));
    }
    fn rest(&mut self, now: f64, actions: &mut Vec<Action>) {
        let mut wait = self.uniform(self.cfg.run.pause_min_s, self.cfg.run.pause_max_s);
        if self.next_break.is_some_and(|at| now >= at) {
            wait += self.uniform(
                self.cfg.behavior.break_min_minutes,
                self.cfg.behavior.break_max_minutes,
            ) * 60.0;
            self.schedule_break(now + wait);
        }
        if self.cfg.game.session_minutes > 0 {
            wait = wait
                .min((self.cfg.game.session_minutes as f64 * 60.0 - self.stats.elapsed_s).max(0.0));
        }
        self.deadline = now + wait;
        self.state = State::Resting;
        self.icon = None;
        actions.push(Action::ResetDetection);
    }
    fn schedule_break(&mut self, now: f64) {
        self.next_break = if self.cfg.behavior.break_every_max_minutes <= 0.0 {
            None
        } else {
            Some(
                now + self.uniform(
                    self.cfg.behavior.break_every_min_minutes,
                    self.cfg.behavior.break_every_max_minutes,
                ) * 60.0,
            )
        };
    }
    fn uniform(&mut self, lo: f64, hi: f64) -> f64 {
        if hi <= lo {
            lo
        } else {
            self.rng.gen_range(lo..=hi)
        }
    }
    fn chance(&mut self, probability: f64) -> bool {
        probability > 0.0 && self.rng.gen::<f64>() < probability
    }
    fn finish(&mut self, reason: &str, actions: &mut Vec<Action>) {
        self.state = State::Stopped;
        actions.push(Action::Finished(reason.into()));
    }
}
