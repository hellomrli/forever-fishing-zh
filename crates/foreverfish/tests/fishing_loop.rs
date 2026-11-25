//! The presence loop, including actual bundled sounds and simulated input delivery.
use foreverfish::{
    assets,
    audio::{self, Bite, Cancel, Detector, RATE},
    config::Config,
    engine::{Action, Engine, Observation, State},
    vision::IconMatch,
};

fn seen() -> Observation {
    Observation {
        icon: Some(IconMatch {
            score: 0.95,
            x: 120,
            y: 100,
            size: 29,
            gold: 0.3,
        }),
        ..Default::default()
    }
}

fn absent() -> Observation {
    Observation {
        icon: Some(IconMatch::none(29)),
        ..Default::default()
    }
}

fn bite() -> Bite {
    Bite {
        clip: "fishbite".into(),
        score: 0.9,
        onset_s: 0.0,
    }
}

fn presses(actions: &[Action]) -> Vec<&str> {
    actions
        .iter()
        .filter_map(|action| match action {
            Action::Press { key, .. } => Some(key.as_str()),
            _ => None,
        })
        .collect()
}

fn engine() -> Engine {
    let mut cfg = Config::default();
    cfg.run.pause_min_s = 0.0;
    cfg.run.pause_max_s = 0.0;
    Engine::with_seed(cfg, 7)
}

fn waiting() -> Engine {
    let mut engine = engine();
    assert_eq!(presses(&engine.tick(0.0, absent())), ["1"]);
    engine.tick(1.5, seen());
    assert_eq!(engine.state, State::Waiting);
    engine
}

#[test]
fn starting_and_resuming_with_a_gear_does_not_cast_jump_or_apply_a_lure() {
    let mut engine = engine();
    engine.cfg.game.lure_key = "2".into();
    engine.cfg.behavior.jump_chance = 1.0;
    assert!(presses(&engine.tick(0.0, seen())).is_empty());
    assert_eq!(engine.state, State::Waiting);
    engine.toggle_pause();
    assert!(presses(&engine.tick(1.0, seen())).is_empty());
    assert_eq!(engine.state, State::Paused);
    engine.toggle_pause();
    engine.tick(2.0, Observation::default());
    assert!(presses(&engine.tick(2.1, seen())).is_empty());
    assert_eq!(engine.state, State::Waiting);
    assert_eq!(engine.stats.casts, 0);
    assert_eq!(engine.stats.misses, 0);
}

#[test]
fn no_screenshot_does_not_mean_the_gear_is_absent() {
    let mut engine = engine();
    for now in [0.0, 5.0, 600.0] {
        assert!(presses(&engine.tick(now, Observation::default())).is_empty());
        assert_eq!(engine.state, State::Idle);
    }
    assert_eq!(presses(&engine.tick(600.1, absent())), ["1"]);
}

#[test]
fn landing_wait_starts_after_actual_delivery_even_with_a_short_configured_timeout() {
    let mut engine = engine();
    engine.cfg.run.icon_timeout_s = 0.5;
    engine.tick(0.0, absent());
    engine.key_pressed(0.4);
    assert!(!engine.wants_vision(1.6));
    engine.tick(1.6, seen());
    assert_eq!(engine.state, State::Casting);
    assert!(engine.wants_vision(2.0));
    engine.tick(2.0, seen());
    assert_eq!(engine.state, State::Waiting);
    assert_eq!(engine.stats.casts, 1);
    assert_eq!(engine.stats.misses, 0);
}

#[test]
fn present_gear_waits_until_the_no_sound_deadline_and_reeling_restarts_it() {
    let mut engine = waiting();
    for now in [10.0, 20.0, 29.0] {
        assert!(presses(&engine.tick(now, seen())).is_empty());
        assert_eq!(engine.state, State::Waiting);
    }
    let actions = engine.tick(
        30.0,
        Observation {
            bite: Some(bite()),
            ..seen()
        },
    );
    assert_eq!(presses(&actions), ["f"]);
    assert_eq!(engine.state, State::Looting);
    assert!(presses(&engine.tick(59.0, seen())).is_empty());
    assert_eq!(engine.state, State::Looting);
    assert_eq!(engine.stats.casts, 1);
    assert_eq!(engine.stats.catches, 0);
    assert_eq!(engine.stats.misses, 0);
    assert_eq!(
        presses(&engine.tick(
            59.1,
            Observation {
                bite: Some(bite()),
                ..seen()
            }
        )),
        ["f"]
    );
    assert!(presses(&engine.tick(89.0, seen())).is_empty());
    assert_eq!(presses(&engine.tick(89.1, seen())), ["1"]);
    assert_eq!(engine.stats.misses, 1);
    assert_eq!(engine.stats.casts, 2);
}

#[test]
fn sound_wins_over_sustained_icon_loss_on_the_same_tick() {
    let mut engine = waiting();
    for now in [5.0, 5.25, 5.5, 5.75] {
        engine.tick(now, absent());
    }
    let actions = engine.tick(
        6.0,
        Observation {
            bite: Some(bite()),
            ..absent()
        },
    );
    assert_eq!(presses(&actions), ["f"]);
    assert_eq!(engine.state, State::Looting);
    assert_eq!(engine.stats.misses, 0);
    assert_eq!(engine.stats.catches, 0);
}

#[test]
fn only_fresh_sustained_absence_ends_a_cast_and_each_reel_counts_once() {
    for reeled in [false, true] {
        let mut engine = waiting();
        if reeled {
            engine.tick(
                2.0,
                Observation {
                    bite: Some(bite()),
                    ..seen()
                },
            );
        }
        for now in [3.0, 3.25, 3.5] {
            engine.tick(now, absent());
        }
        assert_eq!(engine.stats.catches, 0);
        assert_eq!(engine.stats.misses, 0);
        // A tick without a screenshot must not count an unverified disappearance.
        engine.tick(3.75, Observation::default());
        assert_eq!(engine.stats.catches, 0);
        assert_eq!(engine.stats.misses, 0);
        engine.tick(4.0, seen());
        for now in [5.0, 5.25, 5.5, 5.75, 6.0] {
            engine.tick(now, absent());
        }
        assert_eq!(engine.state, State::Resting);
        assert_eq!(engine.stats.catches, u64::from(reeled));
        assert_eq!(engine.stats.misses, u64::from(!reeled));
        engine.tick(6.1, absent());
        assert_eq!(presses(&engine.tick(6.2, absent())), ["1"]);
        assert_eq!(engine.stats.casts, 2);
        assert_eq!(engine.stats.catches, u64::from(reeled));
    }
}

#[test]
fn missing_gear_after_landing_retries_but_unobserved_absence_does_not() {
    let mut engine = engine();
    engine.tick(0.0, absent());
    engine.tick(4.1, Observation::default());
    assert_eq!(engine.state, State::Casting);
    engine.tick(4.2, absent());
    assert_eq!(engine.state, State::Resting);
    assert_eq!(engine.stats.misses, 1);
}

#[test]
fn cancelled_input_and_focus_loss_pause_then_recheck_presence() {
    let mut engine = engine();
    engine.tick(0.0, absent());
    let actions = engine.key_cancelled();
    assert!(presses(&actions).is_empty());
    assert_eq!(engine.stats.casts, 0);
    engine.tick(0.2, Observation::default());
    assert!(presses(&engine.tick(0.3, seen())).is_empty());
    assert_eq!(engine.state, State::Waiting);
    assert_eq!(engine.stats.misses, 0);
    assert!(presses(&engine.tick(
        1.0,
        Observation {
            focused: false,
            bite: Some(bite()),
            ..seen()
        }
    ))
    .is_empty());
    assert_eq!(engine.state, State::Paused);
    engine.stop();
    assert!(presses(&engine.tick(1.1, absent())).is_empty());
    assert_eq!(engine.state, State::Stopped);
}

#[test]
fn interrupted_lure_is_reapplied_and_its_wait_starts_after_delivery() {
    let mut engine = engine();
    engine.cfg.game.lure_key = "2".into();
    assert_eq!(presses(&engine.tick(0.0, absent())), ["2"]);
    engine.key_pressed(1.0);
    engine.tick(6.1, absent());
    assert_eq!(engine.state, State::Lure);
    engine.tick(
        6.2,
        Observation {
            focused: false,
            ..Default::default()
        },
    );
    engine.tick(7.0, Observation::default());
    assert_eq!(presses(&engine.tick(7.1, absent())), ["2"]);
    engine.tick(13.2, Observation::default());
    assert_eq!(engine.state, State::Idle);
    // Even after a lure or jump, a newly visible gear prevents the cast.
    assert!(presses(&engine.tick(13.3, seen())).is_empty());
    assert_eq!(engine.state, State::Waiting);
}

#[test]
fn resize_preserves_the_cast_and_rechecks_the_gear() {
    let mut engine = waiting();
    assert!(presses(&engine.tick(
        5.0,
        Observation {
            resized: true,
            ..seen()
        }
    ))
    .is_empty());
    assert_eq!(engine.state, State::Waiting);
    assert_eq!(engine.stats.misses, 0);
}

#[test]
fn session_quit_waits_until_the_current_gear_disappears() {
    let mut engine = engine();
    engine.cfg.game.session_minutes = 1;
    engine.cfg.game.quit_key = "3".into();
    engine.tick(0.0, Observation::default());
    engine.tick(50.0, absent());
    engine.tick(51.5, seen());
    assert!(presses(&engine.tick(60.0, seen())).is_empty());
    assert_eq!(
        presses(&engine.tick(
            60.1,
            Observation {
                bite: Some(bite()),
                ..seen()
            }
        )),
        ["f"]
    );
    for now in [60.2, 60.45, 60.7, 60.95, 61.2] {
        engine.tick(now, absent());
    }
    assert_eq!(engine.state, State::Resting);
    assert_eq!(presses(&engine.tick(61.3, absent())), ["3"]);
    assert_eq!(engine.state, State::Quitting);
}

#[test]
fn paused_session_limits_never_send_a_quit_key() {
    let mut engine = waiting();
    engine.cfg.game.session_minutes = 1;
    engine.cfg.game.quit_key = "3".into();
    engine.toggle_pause();
    engine.tick(2.0, seen());
    assert!(presses(&engine.tick(60.0, seen())).is_empty());
    assert_eq!(engine.state, State::Stopped);
}

#[test]
fn legacy_visual_timing_and_skip_values_do_not_block_bundled_sounds() {
    let config = Config::parse(
        "[vision]\nconfirm_bite=true\nconfirm_window_s=2.0\nmotion_threshold=255.0\n\
         [run]\ncast_timeout_s=30.0\nloot_timeout_s=0.1\nloot_confirm_s=10.0\n\
         [behavior]\nreaction_min_ms=10000\nreaction_max_ms=10000\nskip_bite_chance=1.0\n",
    )
    .unwrap();
    assert_eq!(config, Config::default());
    for &(clip, bytes) in assets::CLIPS {
        let mut engine = Engine::with_seed(config.clone(), 7);
        engine.tick(0.0, seen());
        let mut detector =
            Detector::new(config.audio.bite_threshold, config.audio.refractory_s).unwrap();
        let (samples, rate) = audio::decode_wav(std::io::Cursor::new(bytes)).unwrap();
        assert_eq!(rate, RATE);
        let mut signal = vec![0.0; 4312];
        signal.extend(samples);
        signal.extend(vec![0.0; 4 * RATE as usize]);
        let mut heard = 0;
        let mut keys = Vec::new();
        for (index, packet) in signal.chunks(1024).enumerate() {
            let feed = detector.feed(packet, &Cancel::default()).unwrap();
            let now = 5.0 + (index * 1024 + packet.len()) as f64 / RATE as f64;
            let detected = !feed.bites.is_empty();
            heard += feed.bites.len();
            for bite in &feed.bites {
                assert_eq!(bite.clip, clip);
            }
            let actions = engine.tick(
                now,
                Observation {
                    bite: feed.bites.first().cloned(),
                    ..seen()
                },
            );
            let pressed = presses(&actions);
            assert_eq!(pressed, if detected { vec!["f"] } else { vec![] });
            keys.extend(pressed.into_iter().map(str::to_owned));
        }
        assert_eq!(heard, 1, "{clip}");
        assert_eq!(keys, ["f"]);
        assert_eq!(engine.state, State::Looting);
        assert_eq!(engine.stats.casts, 0);
        assert_eq!(engine.stats.catches, 0);
    }
}

#[test]
fn no_sound_recasts_on_the_deadline_even_with_a_gear_or_without_a_screenshot() {
    for observed in [seen(), Observation::default()] {
        let mut engine = waiting();
        assert!(presses(&engine.tick(29.99, seen())).is_empty());
        assert_eq!(presses(&engine.tick(30.0, observed)), ["1"]);
        assert_eq!(engine.state, State::Casting);
        assert_eq!(engine.stats.casts, 2);
        assert_eq!(engine.stats.misses, 1);
        engine.key_pressed(30.4);
        engine.tick(31.9, seen());
        assert!(presses(&engine.tick(60.39, seen())).is_empty());
        assert_eq!(presses(&engine.tick(60.41, seen())), ["1"]);
    }
}

#[test]
fn no_sound_deadline_applies_while_still_seeking_the_first_gear() {
    let mut engine = engine();
    engine.cfg.run.icon_timeout_s = 60.0;
    engine.cfg.run.pause_min_s = 2.0;
    engine.cfg.run.pause_max_s = 2.0;
    assert_eq!(presses(&engine.tick(0.0, absent())), ["1"]);
    engine.key_pressed(0.4);
    for now in [1.9, 20.0, 30.39] {
        assert!(presses(&engine.tick(now, absent())).is_empty());
        assert_eq!(engine.state, State::Casting);
    }
    assert!(!engine.wants_vision(30.41));
    assert_eq!(presses(&engine.tick(30.41, Observation::default())), ["1"]);
    assert_eq!(engine.state, State::Casting);
    assert_eq!(engine.stats.casts, 2);
    assert_eq!(engine.stats.misses, 1);
}

#[test]
fn no_sound_deadline_overrides_icon_loss_when_a_capture_finishes_late() {
    for reeled in [false, true] {
        let mut engine = waiting();
        engine.cfg.run.pause_min_s = 2.0;
        engine.cfg.run.pause_max_s = 2.0;
        let deadline = if reeled {
            engine.tick(
                2.0,
                Observation {
                    bite: Some(bite()),
                    ..seen()
                },
            );
            32.0
        } else {
            30.0
        };
        for offset in [1.0, 0.75, 0.5, 0.25] {
            assert!(presses(&engine.tick(deadline - offset, absent())).is_empty());
        }
        // The scan began before the deadline; its absent result arrives after it.
        assert!(engine.wants_vision(deadline - 0.01));
        assert_eq!(presses(&engine.tick(deadline + 0.02, absent())), ["1"]);
        assert_eq!(engine.state, State::Casting);
        assert_eq!(engine.stats.casts, 2);
        assert_eq!(engine.stats.misses, 1);
        assert_eq!(engine.stats.catches, 0);
    }
}

#[test]
fn no_sound_timer_includes_landing_and_uses_actual_key_delivery() {
    let mut engine = engine();
    engine.tick(0.0, absent());
    engine.key_pressed(0.4);
    engine.tick(2.0, seen());
    assert!(presses(&engine.tick(30.39, seen())).is_empty());
    assert_eq!(presses(&engine.tick(30.41, seen())), ["1"]);
}

#[test]
fn disappearing_gear_still_allows_real_sounds_with_twenty_millisecond_packets() {
    for &(clip, bytes) in assets::CLIPS {
        let mut engine = engine();
        engine.tick(0.0, seen());
        let mut detector = Detector::new(0.66, 3.0).unwrap();
        let (mut signal, rate) = audio::decode_wav(std::io::Cursor::new(bytes)).unwrap();
        assert_eq!(rate, RATE);
        signal.extend(vec![0.0; 2 * RATE as usize]);
        let mut keys = Vec::new();
        let mut last_vision = 4.75;
        for (index, packet) in signal.chunks(441).enumerate() {
            let now = 5.0 + (index * 441 + packet.len()) as f64 / RATE as f64;
            let result = detector.feed(packet, &Cancel::default()).unwrap();
            let mut observed = Observation {
                bite: result.bites.first().cloned(),
                ..Default::default()
            };
            if now - last_vision >= 0.25 {
                last_vision = now;
                observed.icon = absent().icon;
            }
            keys.extend(
                presses(&engine.tick(now, observed))
                    .into_iter()
                    .map(str::to_owned),
            );
        }
        assert_eq!(keys.first().map(String::as_str), Some("f"), "{clip}");
        assert_eq!(keys.iter().filter(|key| *key == "f").count(), 1);
        assert_eq!(engine.stats.bites, 1);
        assert_eq!(engine.stats.catches, 1);
    }
}
