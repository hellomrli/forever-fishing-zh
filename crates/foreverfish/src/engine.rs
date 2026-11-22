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
    Reacting,
    Looting,
    Resting,
    Paused,
    Quitting,
    Stopped,
}

impl State {
    pub fn label(self) -> &'static str {
        match self {
            Self::Idle => "准备抛竿",
            Self::Lure => "正在补饵",
            Self::Jump => "等待落地",
            Self::Casting => "寻找金色齿轮",
            Self::Waiting => "等待咬钩",
            Self::Reacting => "准备收杆",
            Self::Looting => "确认收杆",
            Self::Resting => "休息等待",
            Self::Paused => "已暂停",
            Self::Quitting => "等待退出游戏",
            Self::Stopped => "已停止",
        }
    }
    fn busy(self) -> bool {
        matches!(
            self,
            Self::Casting | Self::Waiting | Self::Reacting | Self::Looting
        )
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
    CaptureBeforeCast,
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
    pub visual_confirmed: bool,
    /// Strongest recent change near the bobber and the bar it had to reach.
    pub motion: Option<(f32, f32)>,
}

/// The icon counts as lost only after this long without a match: the splash is recognised
/// about 0.9 s after it starts, so a brief flicker must not recast first (Python: 0.75 s).
const ICON_LOST_S: f64 = 0.75;
pub const MEASURE_AFTER_S: f64 = 1.5;
const VISUAL_MISSES_BEFORE_BYPASS: u32 = 2;

impl Default for Observation {
    fn default() -> Self {
        Self {
            alive: true,
            focused: true,
            resized: false,
            icon: None,
            bite: None,
            visual_confirmed: false,
            motion: None,
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
    interact_at: f64,
    lure_at: Option<f64>,
    disappeared: Option<f64>,
    misses: usize,
    candidate: Option<(Bite, f64)>,
    bite_rejected: bool,
    visual_misses: u32,
    visual_bypassed: bool,
    resume_busy: bool,
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
            interact_at: 0.0,
            lure_at: None,
            disappeared: None,
            misses: 0,
            candidate: None,
            bite_rejected: false,
            visual_misses: 0,
            visual_bypassed: false,
            resume_busy: false,
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
                let elapsed = now - self.cast_at;
                elapsed >= MEASURE_AFTER_S && elapsed <= self.cfg.run.icon_timeout_s
            }
            State::Waiting | State::Looting => true,
            _ => false,
        }
    }
    pub fn visual_confirmation_enabled(&self) -> bool {
        self.cfg.vision.confirm_bite && !self.visual_bypassed
    }
    pub fn reset_audio(&mut self) {
        self.candidate = None;
        self.bite_rejected = false;
    }
    /// Capture and key delivery may take time; gameplay deadlines start after delivery.
    pub fn key_pressed(&mut self, now: f64) {
        match self.state {
            State::Casting => self.cast_at = now,
            State::Looting => self.interact_at = now,
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
            self.resume_busy = self.state.busy() && self.state != State::Casting;
            self.state = State::Paused;
            self.icon = None;
            self.reset_audio();
            actions.push(Action::Message(
                "按键发送前游戏失去焦点或已暂停；继续后重新抛竿".into(),
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
                self.resume_busy = self.state.busy();
                self.state = State::Paused;
                self.reset_audio();
                actions.push(Action::Message(
                    if self.manual_paused {
                        "手动暂停；继续后重新抛竿"
                    } else {
                        "游戏未在前台或已最小化，切回后重新抛竿"
                    }
                    .into(),
                ));
                actions.push(Action::ResetDetection);
            }
            return actions;
        }
        if self.state == State::Paused {
            if self.resume_busy {
                self.stats.misses += 1;
            }
            self.resume_busy = false;
            self.icon = None;
            self.candidate = None;
            self.state = State::Idle;
            actions.push(Action::ResetDetection);
            actions.push(Action::Message("已恢复，准备重新抛竿".into()));
            return actions;
        }
        if observed.resized && self.state.busy() {
            self.miss(now, "窗口尺寸改变，重新识别浮漂", &mut actions);
            return actions;
        }
        match self.state {
            State::Idle => {
                if !self.cfg.game.lure_key.trim().is_empty()
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
            State::Jump if now >= self.deadline => self.cast(now, &mut actions),
            State::Casting => {
                if now - self.cast_at > self.cfg.run.icon_timeout_s {
                    self.visual_misses = 0;
                    self.miss(
                        now,
                        "未找到金色齿轮，请检查命令、校准和水面范围",
                        &mut actions,
                    );
                } else if let Some(icon) = observed.icon.filter(|m| {
                    now - self.cast_at >= MEASURE_AFTER_S
                        && m.accepted(self.cfg.vision.icon_threshold)
                }) {
                    self.icon = Some(icon);
                    self.misses = 0;
                    self.candidate = None;
                    self.disappeared = None;
                    self.state = State::Waiting;
                    actions.push(Action::ResetDetection);
                    actions.push(Action::Message(format!(
                        "找到齿轮（{} 像素，得分 {:.2}），等待水花",
                        icon.size, icon.score
                    )));
                }
            }
            State::Waiting => {
                if let Some(bite) = observed.bite {
                    self.stats.bites += 1;
                    actions.push(Action::Message(format!(
                        "听到 {} 水花，得分 {:.2}",
                        bite.clip, bite.score
                    )));
                    self.candidate = Some((bite, now + self.cfg.vision.confirm_window_s));
                }
                if let Some((bite, until)) = &self.candidate {
                    if !self.visual_confirmation_enabled() || observed.visual_confirmed {
                        let score = bite.score;
                        self.candidate = None;
                        self.visual_misses = 0;
                        if self.chance(self.cfg.behavior.skip_bite_chance) {
                            self.miss(now, "按可选行为设置跳过本次咬钩", &mut actions);
                        } else {
                            self.state = State::Reacting;
                            self.deadline = now
                                + self.uniform(
                                    self.cfg.behavior.reaction_min_ms as f64,
                                    self.cfg.behavior.reaction_max_ms as f64,
                                ) / 1000.0;
                            actions.push(Action::Message(if self.visual_confirmation_enabled() {
                                format!("水花与画面确认通过（{score:.2}），准备互动")
                            } else {
                                format!("识别到水花（{score:.2}），准备互动")
                            }));
                        }
                    } else if now >= *until {
                        self.candidate = None;
                        self.bite_rejected = true;
                        actions.push(Action::Message(match observed.motion {
                            Some((change, bar)) => format!(
                                "水花缺少局部画面变化（最大 {change:.1}，需要 {bar:.1}），继续等待"
                            ),
                            None => "水花缺少局部画面变化（尚无画面样本），继续等待".into(),
                        }));
                    }
                }
                if self.state == State::Waiting {
                    if let Some(icon) = observed.icon {
                        if icon.accepted(self.cfg.vision.icon_threshold) {
                            self.icon = Some(icon);
                            self.misses = 0;
                            self.disappeared = None;
                        } else {
                            self.misses += 1;
                            self.disappeared.get_or_insert(now);
                        }
                    }
                    if self.misses >= 3
                        && self.disappeared.is_some_and(|at| now - at >= ICON_LOST_S)
                    {
                        self.note_visual_miss(&mut actions);
                        self.miss(now, "浮漂图标连续丢失，重新抛竿", &mut actions);
                    } else if now - self.cast_at > self.cfg.run.cast_timeout_s {
                        self.note_visual_miss(&mut actions);
                        self.miss(now, "等待咬钩超时", &mut actions);
                    }
                }
            }
            State::Reacting if now >= self.deadline => {
                let key = self.cfg.game.interact_key.clone();
                self.press(key, &mut actions);
                self.state = State::Looting;
                self.interact_at = now;
                self.misses = 0;
                self.disappeared = None;
                actions.push(Action::Message("已按互动键，等待图标连续消失".into()));
            }
            State::Looting => {
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
                        && self
                            .disappeared
                            .is_some_and(|at| now - at >= self.cfg.run.loot_confirm_s)
                    {
                        self.stats.catches += 1;
                        self.rest(now, &mut actions);
                        actions.push(Action::Message("图标连续消失，估计收杆 +1".into()));
                    }
                }
                if self.state == State::Looting
                    && now - self.interact_at > self.cfg.run.loot_timeout_s
                {
                    self.miss(now, "互动后图标没有连续消失", &mut actions);
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

    fn press(&mut self, key: String, actions: &mut Vec<Action>) {
        let jitter = self.cfg.behavior.key_hold_jitter_ms as f64;
        let hold = (self.cfg.run.key_hold_ms as f64 + self.uniform(-jitter, jitter)).round() as u32;
        actions.push(Action::Press { key, hold_ms: hold });
    }
    fn cast(&mut self, now: f64, actions: &mut Vec<Action>) {
        self.icon = None;
        self.candidate = None;
        self.bite_rejected = false;
        self.disappeared = None;
        self.misses = 0;
        actions.push(Action::ResetDetection);
        actions.push(Action::CaptureBeforeCast);
        let key = self.cfg.game.cast_key.clone();
        self.press(key, actions);
        self.cast_at = now;
        self.stats.casts += 1;
        self.state = State::Casting;
        actions.push(Action::Message("已抛竿，等待浮漂落水后寻找金色齿轮".into()));
    }
    fn miss(&mut self, now: f64, message: &str, actions: &mut Vec<Action>) {
        self.stats.misses += 1;
        self.rest(now, actions);
        actions.push(Action::Message(message.into()));
    }
    fn note_visual_miss(&mut self, actions: &mut Vec<Action>) {
        if !self.visual_confirmation_enabled() {
            return;
        }
        if !self.bite_rejected && self.candidate.is_none() {
            self.visual_misses = 0;
            return;
        }
        self.visual_misses += 1;
        if self.visual_misses >= VISUAL_MISSES_BEFORE_BYPASS {
            self.visual_bypassed = true;
            actions.push(Action::Message(format!(
                "画面确认已连续 {} 竿拦下水花且没有收杆，本次运行改为听到水花就收杆；可调低画面变化阈值或关闭画面确认",
                self.visual_misses
            )));
        }
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
        self.candidate = None;
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

#[cfg(test)]
mod tests {
    use super::*;
    fn icon() -> IconMatch {
        IconMatch {
            score: 0.95,
            x: 120,
            y: 100,
            size: 29,
            gold: 0.3,
        }
    }
    fn looting() -> Engine {
        let mut cfg = Config::default();
        cfg.vision.confirm_bite = false;
        let mut engine = Engine::with_seed(cfg, 42);
        engine.tick(0.0, Observation::default());
        engine.tick(
            1.5,
            Observation {
                icon: Some(icon()),
                ..Default::default()
            },
        );
        engine.tick(
            2.0,
            Observation {
                bite: Some(Bite {
                    clip: "test".into(),
                    score: 0.9,
                    onset_s: 0.0,
                }),
                ..Default::default()
            },
        );
        engine.tick(2.01, Observation::default());
        assert_eq!(engine.state, State::Looting);
        engine
    }
    #[test]
    fn single_miss_and_recovery_do_not_count_a_catch() {
        let mut engine = looting();
        engine.tick(
            2.1,
            Observation {
                icon: Some(IconMatch::none(29)),
                ..Default::default()
            },
        );
        engine.tick(
            2.4,
            Observation {
                icon: Some(icon()),
                ..Default::default()
            },
        );
        assert_eq!(engine.stats.catches, 0);
        for now in [2.5, 2.7, 3.0] {
            engine.tick(
                now,
                Observation {
                    icon: Some(IconMatch::none(29)),
                    ..Default::default()
                },
            );
        }
        assert_eq!(engine.stats.catches, 1);
        assert_eq!(engine.stats.casts, 1);
        assert_eq!(engine.stats.bites, 1);
    }
    #[test]
    fn brief_icon_flicker_while_waiting_does_not_recast_before_the_bite() {
        let mut cfg = Config::default();
        cfg.vision.confirm_bite = false;
        let mut engine = Engine::with_seed(cfg, 7);
        engine.tick(0.0, Observation::default());
        engine.tick(
            1.5,
            Observation {
                icon: Some(icon()),
                ..Default::default()
            },
        );
        // The splash hides the icon for half a second; the bite is heard after that.
        for now in [5.0, 5.1, 5.2, 5.3, 5.4, 5.5] {
            engine.tick(
                now,
                Observation {
                    icon: Some(IconMatch::none(29)),
                    ..Default::default()
                },
            );
        }
        assert_eq!(engine.state, State::Waiting);
        engine.tick(
            5.6,
            Observation {
                bite: Some(Bite {
                    clip: "own".into(),
                    score: 0.9,
                    onset_s: 0.0,
                }),
                ..Default::default()
            },
        );
        assert_eq!(engine.state, State::Reacting);
        assert_eq!(engine.stats.misses, 0);
    }
    #[test]
    fn icon_missing_for_long_while_waiting_recasts() {
        let mut engine = Engine::with_seed(Config::default(), 7);
        engine.tick(0.0, Observation::default());
        engine.tick(
            1.5,
            Observation {
                icon: Some(icon()),
                ..Default::default()
            },
        );
        for step in 0..9 {
            engine.tick(
                5.0 + step as f64 * 0.1,
                Observation {
                    icon: Some(IconMatch::none(29)),
                    ..Default::default()
                },
            );
        }
        assert_eq!(engine.state, State::Resting);
        assert_eq!(engine.stats.misses, 1);
    }
    #[test]
    fn interrupted_lure_is_reapplied_and_long_wait_can_stop() {
        let mut cfg = Config::default();
        cfg.game.lure_key = "2".into();
        cfg.run.lure_cast_s = 120.0;
        let mut engine = Engine::with_seed(cfg, 1);
        engine.tick(0.0, Observation::default());
        assert_eq!(engine.state, State::Lure);
        engine.tick(
            2.0,
            Observation {
                focused: false,
                ..Default::default()
            },
        );
        assert_eq!(engine.state, State::Paused);
        engine.tick(5.0, Observation::default());
        let actions = engine.tick(5.1, Observation::default());
        assert!(actions
            .iter()
            .any(|a| matches!(a, Action::Press { key, .. } if key == "2")));
        engine.stop();
        engine.tick(5.2, Observation::default());
        assert_eq!(engine.state, State::Stopped);
        assert_eq!(engine.stats.casts, 0);
    }
    #[test]
    fn visual_confirmation_filters_splashes_and_focus_pauses_reaction() {
        let mut engine = Engine::with_seed(Config::default(), 1);
        engine.tick(0.0, Observation::default());
        engine.tick(
            1.5,
            Observation {
                icon: Some(icon()),
                ..Default::default()
            },
        );
        engine.tick(
            2.0,
            Observation {
                bite: Some(Bite {
                    clip: "other".into(),
                    score: 0.9,
                    onset_s: 0.0,
                }),
                ..Default::default()
            },
        );
        engine.tick(2.7, Observation::default());
        assert_eq!(engine.state, State::Waiting);
        engine.tick(
            3.0,
            Observation {
                bite: Some(Bite {
                    clip: "own".into(),
                    score: 0.9,
                    onset_s: 0.0,
                }),
                visual_confirmed: true,
                ..Default::default()
            },
        );
        assert_eq!(engine.state, State::Reacting);
        let actions = engine.tick(
            3.01,
            Observation {
                focused: false,
                ..Default::default()
            },
        );
        assert_eq!(engine.state, State::Paused);
        assert!(!actions.iter().any(|a| matches!(a, Action::Press { .. })));
    }
    #[test]
    fn limit_defers_quit_until_current_cast_finishes() {
        let mut engine = looting();
        engine.cfg.game.session_minutes = 1;
        engine.cfg.game.quit_key = "3".into();
        engine.tick(
            60.0,
            Observation {
                icon: Some(icon()),
                ..Default::default()
            },
        );
        assert_ne!(engine.state, State::Quitting);
        let actions = engine.tick(60.1, Observation::default());
        assert_eq!(engine.state, State::Quitting);
        assert!(actions
            .iter()
            .any(|a| matches!(a, Action::Press { key, .. } if key == "3")));
    }
    #[test]
    fn paused_and_unfocused_session_limits_never_send_quit_key() {
        let mut cfg = Config::default();
        cfg.game.session_minutes = 1;
        cfg.game.quit_key = "3".into();
        let mut engine = Engine::with_seed(cfg, 1);
        engine.tick(0.0, Observation::default());
        engine.toggle_pause();
        engine.tick(1.0, Observation::default());
        let actions = engine.tick(60.0, Observation::default());
        assert_eq!(engine.state, State::Stopped);
        assert!(!actions.iter().any(|a| matches!(a, Action::Press { .. })));
    }

    #[test]
    fn landing_wait_starts_after_key_delivery_and_remains_interruptible() {
        let mut engine = Engine::with_seed(Config::default(), 1);
        engine.tick(0.0, Observation::default());
        engine.key_pressed(0.4); // The pre-cast capture and key hold took time.
        engine.tick(
            1.6,
            Observation {
                icon: Some(icon()),
                ..Default::default()
            },
        );
        assert_eq!(engine.state, State::Casting);
        assert!(!engine.wants_vision(1.6));
        assert!(engine.wants_vision(2.0));
        engine.toggle_pause();
        let actions = engine.tick(1.7, Observation::default());
        assert_eq!(engine.state, State::Paused);
        assert!(!actions.iter().any(|a| matches!(a, Action::Press { .. })));
    }

    #[test]
    fn cancelled_cast_is_not_counted_and_can_resume_without_a_miss() {
        let mut engine = Engine::with_seed(Config::default(), 1);
        engine.tick(0.0, Observation::default());
        let actions = engine.key_cancelled();
        assert_eq!(engine.state, State::Paused);
        assert_eq!(engine.stats.casts, 0);
        assert!(!actions.iter().any(|a| matches!(a, Action::Press { .. })));
        engine.tick(0.2, Observation::default());
        engine.tick(0.3, Observation::default());
        assert_eq!(engine.state, State::Casting);
        assert_eq!(engine.stats.casts, 1);
        assert_eq!(engine.stats.misses, 0);
    }

    #[test]
    fn short_icon_timeout_expires_without_accepting_a_prelanding_icon() {
        let mut cfg = Config::default();
        cfg.run.icon_timeout_s = 0.5;
        let mut engine = Engine::with_seed(cfg, 1);
        engine.tick(0.0, Observation::default());
        assert!(!engine.wants_vision(0.4));
        engine.tick(
            0.6,
            Observation {
                icon: Some(icon()),
                ..Default::default()
            },
        );
        assert_eq!(engine.state, State::Resting);
        assert_eq!(engine.stats.misses, 1);
    }

    fn lost_cast(engine: &mut Engine, at: f64, heard: bool) -> Vec<Action> {
        assert_eq!(engine.state, State::Idle);
        engine.tick(at, Observation::default());
        engine.tick(
            at + 1.5,
            Observation {
                icon: Some(icon()),
                ..Default::default()
            },
        );
        if heard {
            engine.tick(
                at + 2.0,
                Observation {
                    bite: Some(Bite {
                        clip: "test".into(),
                        score: 0.9,
                        onset_s: 0.0,
                    }),
                    ..Default::default()
                },
            );
        }
        let actions = engine.tick(
            at + 3.1,
            Observation {
                icon: Some(icon()),
                ..Default::default()
            },
        );
        assert_eq!(engine.state, State::Resting);
        engine.tick(at + 3.2, Observation::default());
        actions
    }

    fn short_cast_config() -> Config {
        let mut cfg = Config::default();
        cfg.run.cast_timeout_s = 3.0;
        cfg.run.pause_min_s = 0.0;
        cfg.run.pause_max_s = 0.0;
        cfg
    }

    #[test]
    fn two_visually_blocked_casts_bypass_the_check_only_for_this_session() {
        // A cast may end before its visual confirmation window expires.
        for confirm_window in [0.6, 2.0] {
            let mut cfg = short_cast_config();
            cfg.vision.confirm_window_s = confirm_window;
            let mut engine = Engine::with_seed(cfg.clone(), 1);
            let mut actions = lost_cast(&mut engine, 0.0, true);
            assert!(engine.visual_confirmation_enabled());
            actions.extend(lost_cast(&mut engine, 4.0, true));
            assert!(!engine.visual_confirmation_enabled());
            assert!(engine.cfg.vision.confirm_bite);
            assert_eq!(
                actions
                    .iter()
                    .filter(|a| matches!(a, Action::Message(m) if m.contains("本次运行改为")))
                    .count(),
                1
            );
            engine.tick(8.0, Observation::default());
            engine.tick(
                9.5,
                Observation {
                    icon: Some(icon()),
                    ..Default::default()
                },
            );
            engine.tick(
                10.0,
                Observation {
                    bite: Some(Bite {
                        clip: "test".into(),
                        score: 0.9,
                        onset_s: 0.0,
                    }),
                    ..Default::default()
                },
            );
            assert_eq!(engine.state, State::Reacting);
            assert!(Engine::new(cfg).visual_confirmation_enabled());
        }
    }

    #[test]
    fn an_unheard_cast_breaks_the_visual_failure_streak() {
        let mut engine = Engine::with_seed(short_cast_config(), 1);
        lost_cast(&mut engine, 0.0, true);
        lost_cast(&mut engine, 4.0, false);
        lost_cast(&mut engine, 8.0, true);
        assert!(engine.visual_confirmation_enabled());
    }

    #[test]
    fn confirmed_bite_resets_visual_failures_and_audio_gaps_discard_pending_bites() {
        let mut engine = Engine::with_seed(short_cast_config(), 1);
        lost_cast(&mut engine, 0.0, true);
        engine.tick(4.0, Observation::default());
        engine.tick(
            5.5,
            Observation {
                icon: Some(icon()),
                ..Default::default()
            },
        );
        engine.tick(
            6.0,
            Observation {
                bite: Some(Bite {
                    clip: "test".into(),
                    score: 0.9,
                    onset_s: 0.0,
                }),
                ..Default::default()
            },
        );
        engine.reset_audio();
        engine.tick(
            6.1,
            Observation {
                visual_confirmed: true,
                ..Default::default()
            },
        );
        assert_eq!(engine.state, State::Waiting);
        engine.tick(
            6.2,
            Observation {
                bite: Some(Bite {
                    clip: "test".into(),
                    score: 0.9,
                    onset_s: 0.0,
                }),
                visual_confirmed: true,
                ..Default::default()
            },
        );
        engine.tick(6.3, Observation::default());
        assert_eq!(engine.state, State::Looting);
        for now in [6.4, 6.6, 6.9] {
            engine.tick(
                now,
                Observation {
                    icon: Some(IconMatch::none(29)),
                    ..Default::default()
                },
            );
        }
        engine.tick(7.0, Observation::default());
        lost_cast(&mut engine, 8.0, true);
        assert!(engine.visual_confirmation_enabled());
        assert_eq!(engine.stats.catches, 1);
    }

    #[test]
    fn lure_wait_starts_after_the_key_is_delivered() {
        let mut cfg = Config::default();
        cfg.game.lure_key = "2".into();
        let mut engine = Engine::with_seed(cfg, 1);
        engine.tick(0.0, Observation::default());
        engine.key_pressed(1.0);
        engine.tick(6.1, Observation::default());
        assert_eq!(engine.state, State::Lure);
        engine.tick(7.0, Observation::default());
        assert_eq!(engine.state, State::Idle);
    }
}
