mod dsp;
mod wav;
pub use dsp::{Bite, Detector, Feed, Fingerprint, Mel, ScoreFrame, BANDS, FFT_SIZE, HOP, RATE};
pub use wav::{analyze_file, analyze_samples, decode_wav, Report, Resampler};

use anyhow::Result;
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

#[derive(Clone, Default, Debug)]
pub struct Cancel(Arc<AtomicBool>);

impl Cancel {
    pub fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
    pub fn check(&self) -> Result<()> {
        anyhow::ensure!(!self.is_cancelled(), "任务已取消");
        Ok(())
    }
    pub fn wait(&self, duration: Duration) -> bool {
        let deadline = Instant::now() + duration;
        while !self.is_cancelled() {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return true;
            }
            std::thread::sleep(left.min(Duration::from_millis(10)));
        }
        false
    }
}

/// Implementations are created, used and dropped in the same worker thread.
pub trait Capture {
    fn name(&self) -> &str;
    fn fallback_reason(&self) -> &str {
        ""
    }
    fn read(&mut self, cancel: &Cancel) -> Result<Vec<f32>>;
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct Device {
    pub id: String,
    pub name: String,
    pub default: bool,
}
