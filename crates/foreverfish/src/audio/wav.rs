use super::{Bite, Cancel, Detector, HOP, RATE};
use anyhow::{ensure, Context, Result};
use hound::{SampleFormat, WavReader};
use serde::Serialize;
use std::{
    collections::VecDeque,
    io::{Read, Seek},
    path::Path,
};

/// Keep the fractional position across packets; never resample each packet independently.
pub struct Resampler {
    source: u32,
    target: u32,
    pending: VecDeque<f32>,
    position: f64,
}

impl Resampler {
    pub fn new(source: u32, target: u32) -> Self {
        assert!(source > 0 && target > 0);
        Self {
            source,
            target,
            pending: VecDeque::new(),
            position: 0.0,
        }
    }
    pub fn feed(&mut self, samples: &[f32]) -> Vec<f32> {
        if self.source == self.target {
            return samples.to_vec();
        }
        self.pending.extend(samples.iter().copied());
        let mut output = Vec::new();
        let step = self.source as f64 / self.target as f64;
        while self.position.floor() as usize + 1 < self.pending.len() {
            let i = self.position.floor() as usize;
            let fraction = (self.position - i as f64) as f32;
            output.push(self.pending[i] * (1.0 - fraction) + self.pending[i + 1] * fraction);
            self.position += step;
        }
        let consumed = (self.position.floor() as usize).min(self.pending.len());
        self.pending.drain(..consumed);
        self.position -= consumed as f64;
        output
    }
    pub fn finish(&mut self) -> Vec<f32> {
        let mut output = Vec::new();
        let step = self.source as f64 / self.target as f64;
        while self.position < self.pending.len() as f64 {
            output.push(self.pending[self.position.floor() as usize]);
            self.position += step;
        }
        self.pending.clear();
        self.position = 0.0;
        output
    }
}

pub fn decode_wav(reader: impl Read + Seek) -> Result<(Vec<f32>, u32)> {
    let mut reader = WavReader::new(reader).context("无法读取 WAV（支持 PCM 或 float）")?;
    let spec = reader.spec();
    ensure!(
        spec.channels > 0 && spec.sample_rate > 0,
        "WAV 声道或采样率无效"
    );
    let values: Vec<f32> = match spec.sample_format {
        SampleFormat::Float => reader
            .samples::<f32>()
            .collect::<std::result::Result<Vec<_>, _>>()?,
        SampleFormat::Int => {
            let denominator = 2f32.powi(spec.bits_per_sample as i32 - 1);
            reader
                .samples::<i32>()
                .map(|s| s.map(|s| s as f32 / denominator))
                .collect::<std::result::Result<Vec<_>, _>>()?
        }
    };
    let samples = values
        .chunks_exact(spec.channels as usize)
        .map(|channels| channels.iter().sum::<f32>() / spec.channels as f32)
        .collect();
    Ok((samples, spec.sample_rate))
}

#[derive(Clone, Debug, Serialize)]
pub struct Report {
    pub duration_s: f64,
    pub peaks: Vec<Bite>,
    pub bites: Vec<Bite>,
    pub floor: f32,
    pub threshold: f32,
}

struct Analysis {
    detector: Detector,
    resampler: Resampler,
    scores: Vec<Vec<f32>>,
    samples: usize,
    rate: u32,
}

impl Analysis {
    fn new(rate: u32) -> Result<Self> {
        let detector = Detector::new(0.66, 3.0)?;
        let scores = vec![Vec::new(); detector.fingerprints.len()];
        Ok(Self {
            detector,
            scores,
            resampler: Resampler::new(rate, RATE),
            samples: 0,
            rate,
        })
    }
    fn feed(&mut self, samples: &[f32], cancel: &Cancel) -> Result<()> {
        self.samples += samples.len();
        let samples = self.resampler.feed(samples);
        self.feed_resampled(&samples, cancel)
    }
    fn feed_resampled(&mut self, samples: &[f32], cancel: &Cancel) -> Result<()> {
        let feed = self.detector.feed(samples, cancel)?;
        for frame in feed.frames {
            for (i, score) in frame.scores.into_iter().enumerate() {
                self.scores[i].push(score);
            }
        }
        Ok(())
    }
    fn finish(mut self, cancel: &Cancel) -> Result<Report> {
        let tail = self.resampler.finish();
        self.feed_resampled(&tail, cancel)?;
        cancel.check()?;
        let length = self.scores.first().map_or(0, Vec::len);
        if length == 0 {
            return Ok(Report {
                duration_s: self.samples as f64 / self.rate as f64,
                peaks: vec![],
                bites: vec![],
                floor: -1.0,
                threshold: 0.66,
            });
        }
        let mut best = vec![-1.0; length];
        let mut which = vec![0; length];
        let mut peaks = Vec::new();
        for (clip, scores) in self.scores.iter().enumerate() {
            for (i, &score) in scores.iter().enumerate() {
                if score > best[i] {
                    best[i] = score;
                    which[i] = clip;
                }
            }
            let (index, &score) = scores
                .iter()
                .enumerate()
                .max_by(|a, b| a.1.total_cmp(b.1))
                .unwrap();
            peaks.push(self.bite(clip, index, score));
        }
        let half = (2.0 * RATE as f64 / HOP as f64) as usize;
        let mut work = best.clone();
        let mut candidates = Vec::new();
        for _ in 0..64 {
            cancel.check()?;
            let (index, &score) = work
                .iter()
                .enumerate()
                .max_by(|a, b| a.1.total_cmp(b.1))
                .unwrap();
            if score < 0.45 {
                break;
            }
            candidates.push((index, self.bite(which[index], index, score)));
            work[index.saturating_sub(half)..(index + half + 1).min(length)].fill(-1.0);
        }
        let mut count = candidates
            .iter()
            .filter(|(_, bite)| bite.score >= 0.65)
            .count();
        while count > 0 {
            let below = candidates
                .get(count)
                .map(|(_, bite)| bite.score)
                .unwrap_or_else(|| work.iter().copied().fold(-1.0, f32::max));
            if candidates[count - 1].1.score - below >= 0.08 {
                break;
            }
            count -= 1;
        }
        for (index, _) in &candidates[..count] {
            // Exclude around the scored frame, not around the estimated onset.
            best[index.saturating_sub(half)..(index + half + 1).min(length)].fill(-1.0);
        }
        let floor = best.iter().copied().fold(-1.0, f32::max);
        let threshold = if count == 0 {
            0.66
        } else {
            ((floor + 0.05) * 100.0).round() / 100.0
        };
        Ok(Report {
            duration_s: self.samples as f64 / self.rate as f64,
            peaks,
            bites: candidates
                .into_iter()
                .take(count)
                .map(|(_, bite)| bite)
                .collect(),
            floor,
            threshold: threshold.clamp(0.5, 0.9),
        })
    }
    fn bite(&self, clip: usize, index: usize, score: f32) -> Bite {
        let fp = &self.detector.fingerprints[clip];
        Bite {
            clip: fp.name.clone(),
            score,
            onset_s: (index * HOP + super::FFT_SIZE) as f64 / RATE as f64
                - (fp.frames.len() * HOP) as f64 / RATE as f64,
        }
    }
}

pub fn analyze_samples(samples: &[f32], rate: u32, cancel: &Cancel) -> Result<Report> {
    ensure!(rate > 0, "采样率必须大于 0");
    let mut analysis = Analysis::new(rate)?;
    for block in samples.chunks(4096) {
        cancel.check()?;
        analysis.feed(block, cancel)?;
    }
    analysis.finish(cancel)
}

pub fn analyze_file(path: &Path, cancel: &Cancel) -> Result<Report> {
    cancel.check()?;
    let mut reader =
        WavReader::open(path).with_context(|| format!("无法打开 WAV {}", path.display()))?;
    let spec = reader.spec();
    ensure!(
        spec.channels > 0 && spec.sample_rate > 0,
        "WAV 声道或采样率无效"
    );
    let mut analysis = Analysis::new(spec.sample_rate)?;
    let mut block = Vec::with_capacity(4096);
    let mut channels = 0usize;
    let mut sum = 0.0f32;
    let mut accept = |value: f32| -> Result<()> {
        sum += value;
        channels += 1;
        if channels == spec.channels as usize {
            block.push(sum / channels as f32);
            sum = 0.0;
            channels = 0;
            if block.len() == 4096 {
                cancel.check()?;
                analysis.feed(&block, cancel)?;
                block.clear();
            }
        }
        Ok(())
    };
    match spec.sample_format {
        SampleFormat::Float => {
            for sample in reader.samples::<f32>() {
                accept(sample?)?;
            }
        }
        SampleFormat::Int => {
            let denominator = 2f32.powi(spec.bits_per_sample as i32 - 1);
            for sample in reader.samples::<i32>() {
                accept(sample? as f32 / denominator)?;
            }
        }
    }
    if !block.is_empty() {
        analysis.feed(&block, cancel)?;
    }
    analysis.finish(cancel)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resampling_retains_fractional_position_across_packets() {
        for rate in [44100, 48000, 16000] {
            let samples: Vec<_> = (0..10000).map(|i| (i as f32 * 0.02).sin()).collect();
            let mut a = Resampler::new(rate, RATE);
            let mut expected = a.feed(&samples);
            expected.extend(a.finish());
            let mut b = Resampler::new(rate, RATE);
            let mut actual = Vec::new();
            for chunk in samples.chunks(73) {
                actual.extend(b.feed(chunk));
            }
            actual.extend(b.finish());
            assert_eq!(actual.len(), expected.len());
            assert!(actual
                .iter()
                .zip(&expected)
                .all(|(a, b)| (a - b).abs() < 1e-4));
        }
    }

    #[test]
    fn recording_report_finds_separated_bundled_splashes() {
        let (samples, rate) = decode_wav(std::io::Cursor::new(crate::assets::CLIPS[0].1)).unwrap();
        let mut recording = vec![0.0; rate as usize];
        recording.extend(&samples);
        recording.extend(vec![0.0; rate as usize * 4]);
        recording.extend(samples);
        recording.extend(vec![0.0; rate as usize * 2]);
        let report = analyze_samples(&recording, rate, &Cancel::default()).unwrap();
        assert_eq!(report.bites.len(), 2);
        // The first onset is 22050 samples into the recording, between 64-sample
        // hops. Python's reference score is 0.979478, while the second is aligned.
        assert!((report.bites[0].score - 1.0).abs() < 1e-5, "{report:?}");
        assert!(
            (report.bites[1].score - 0.9794782).abs() < 1e-5,
            "{report:?}"
        );
        assert!(report.floor < 0.65);
    }
}
