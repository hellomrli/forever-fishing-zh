use super::Cancel;
use anyhow::Result;
use rustfft::{num_complex::Complex, Fft, FftPlanner};
use serde::Serialize;
use std::{collections::VecDeque, f32::consts::PI, sync::Arc};

pub const RATE: u32 = 22050;
pub const FFT_SIZE: usize = 512;
pub const HOP: usize = 64;
pub const BANDS: usize = 40;
type Frame = [f32; BANDS];

pub struct Mel {
    fft: Arc<dyn Fft<f32>>,
    window: Vec<f32>,
    filters: Vec<Vec<(usize, f32)>>,
    buffer: Vec<Complex<f32>>,
    scratch: Vec<Complex<f32>>,
}

impl Default for Mel {
    fn default() -> Self {
        Self::new()
    }
}

impl Mel {
    pub fn new() -> Self {
        let fft = FftPlanner::new().plan_fft_forward(FFT_SIZE);
        let to_mel = |hz: f64| 2595.0 * (1.0 + hz / 700.0).log10();
        let lo = to_mel(300.0);
        let hi = to_mel(8000.0);
        let bins: Vec<_> = (0..BANDS + 2)
            .map(|i| {
                let mel = lo + (hi - lo) * i as f64 / (BANDS + 1) as f64;
                let hz = 700.0 * (10.0f64.powf(mel / 2595.0) - 1.0);
                (((FFT_SIZE + 1) as f64 * hz / RATE as f64).floor() as usize).min(FFT_SIZE / 2)
            })
            .collect();
        let filters = (0..BANDS)
            .map(|i| {
                let left = bins[i];
                let center = bins[i + 1].max(left + 1);
                let right = bins[i + 2].max(center + 1);
                (left..right.min(FFT_SIZE / 2 + 1))
                    .map(|bin| {
                        let weight = if bin < center {
                            (bin - left) as f32 / (center - left) as f32
                        } else {
                            (right - bin) as f32 / (right - center) as f32
                        };
                        (bin, weight)
                    })
                    .collect()
            })
            .collect();
        let scratch = vec![Complex::default(); fft.get_inplace_scratch_len()];
        Self {
            fft,
            filters,
            scratch,
            window: (0..FFT_SIZE)
                .map(|i| 0.5 - 0.5 * (2.0 * PI * i as f32 / (FFT_SIZE - 1) as f32).cos())
                .collect(),
            buffer: vec![Complex::default(); FFT_SIZE],
        }
    }

    pub fn frame(&mut self, samples: &[f32]) -> Frame {
        for (i, sample) in samples.iter().take(FFT_SIZE).enumerate() {
            self.buffer[i] = Complex::new(sample * self.window[i], 0.0);
        }
        self.fft
            .process_with_scratch(&mut self.buffer, &mut self.scratch);
        let mut frame = [0.0; BANDS];
        for (i, filter) in self.filters.iter().enumerate() {
            let power: f32 = filter
                .iter()
                .map(|&(bin, weight)| self.buffer[bin].norm_sqr() * weight)
                .sum();
            frame[i] = 10.0 * (power + 1e-10).log10();
        }
        frame
    }

    pub fn features(&mut self, samples: &[f32], cancel: &Cancel) -> Result<Vec<Frame>> {
        let mut frames = Vec::new();
        if samples.len() < FFT_SIZE {
            cancel.check()?;
            return Ok(frames);
        }
        for (i, start) in (0..=samples.len() - FFT_SIZE).step_by(HOP).enumerate() {
            if i % 64 == 0 {
                cancel.check()?;
            }
            frames.push(self.frame(&samples[start..start + FFT_SIZE]));
        }
        Ok(frames)
    }
}

#[derive(Clone)]
pub struct Fingerprint {
    pub name: String,
    pub frames: Vec<Frame>,
}

impl Fingerprint {
    pub fn bundled() -> Result<Vec<Self>> {
        crate::assets::CLIPS
            .iter()
            .map(|(name, bytes)| {
                let (samples, rate) = super::decode_wav(std::io::Cursor::new(*bytes))?;
                let mut resampler = super::Resampler::new(rate, RATE);
                let mut samples = resampler.feed(&samples);
                samples.extend(resampler.finish());
                samples.truncate((0.8 * RATE as f64) as usize);
                let mut frames = Mel::new().features(&samples, &Cancel::default())?;
                let mut norm = 0.0f64;
                for frame in &mut frames {
                    let mean = frame.iter().sum::<f32>() / BANDS as f32;
                    for value in frame {
                        *value -= mean;
                        norm += (*value as f64).powi(2);
                    }
                }
                let norm = norm.sqrt() as f32 + 1e-9;
                for frame in &mut frames {
                    for value in frame {
                        *value /= norm;
                    }
                }
                anyhow::ensure!(frames.len() >= 2, "内置水花片段过短：{name}");
                Ok(Self {
                    name: (*name).into(),
                    frames,
                })
            })
            .collect()
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Bite {
    pub clip: String,
    pub score: f32,
    pub onset_s: f64,
}

#[derive(Clone, Debug)]
pub struct ScoreFrame {
    pub index: usize,
    pub scores: Vec<f32>,
}

pub struct Feed {
    pub bites: Vec<Bite>,
    pub frames: Vec<ScoreFrame>,
    pub level_dbfs: f32,
}

pub struct Detector {
    pub fingerprints: Vec<Fingerprint>,
    pub threshold: f32,
    refractory: f64,
    mel: Mel,
    pending: VecDeque<f32>,
    frames: VecDeque<Frame>,
    levels: VecDeque<f32>,
    armed: Vec<Option<(Bite, usize)>>,
    quiet_until: f64,
    total: usize,
    max_length: usize,
}

impl Detector {
    pub fn new(threshold: f32, refractory: f64) -> Result<Self> {
        let fingerprints = Fingerprint::bundled()?;
        let max_length = fingerprints.iter().map(|fp| fp.frames.len()).max().unwrap();
        Ok(Self {
            armed: vec![None; fingerprints.len()],
            fingerprints,
            threshold,
            refractory,
            mel: Mel::new(),
            pending: VecDeque::new(),
            frames: VecDeque::new(),
            levels: VecDeque::new(),
            quiet_until: f64::NEG_INFINITY,
            total: 0,
            max_length,
        })
    }

    pub fn reset(&mut self) {
        self.pending.clear();
        self.frames.clear();
        self.levels.clear();
        self.armed.fill(None);
        self.quiet_until = f64::NEG_INFINITY;
    }

    pub fn feed(&mut self, samples: &[f32], cancel: &Cancel) -> Result<Feed> {
        cancel.check()?;
        let rms = if samples.is_empty() {
            0.0
        } else {
            (samples.iter().map(|v| v * v).sum::<f32>() / samples.len() as f32).sqrt()
        };
        let mut result = Feed {
            bites: Vec::new(),
            frames: Vec::new(),
            level_dbfs: 20.0 * (rms + 1e-9).log10(),
        };
        self.pending.extend(samples.iter().copied());
        while self.pending.len() >= FFT_SIZE {
            if self.total.is_multiple_of(16) {
                cancel.check()?;
            }
            let slice = self.pending.make_contiguous();
            let mut frame = self.mel.frame(&slice[..FFT_SIZE]);
            let mean = frame.iter().sum::<f32>() / BANDS as f32;
            for value in &mut frame {
                *value -= mean;
            }
            self.frames.push_back(frame);
            self.levels.push_back(mean);
            if self.frames.len() > self.max_length {
                self.frames.pop_front();
                self.levels.pop_front();
            }
            self.pending.drain(..HOP);
            let index = self.total;
            self.total += 1;
            let end = (index * HOP + FFT_SIZE) as f64 / RATE as f64;
            let mut scores = Vec::with_capacity(self.fingerprints.len());
            let mut ready: Vec<Bite> = Vec::new();
            for (i, fp) in self.fingerprints.iter().enumerate() {
                let length = fp.frames.len();
                let offset = self.frames.len().saturating_sub(length);
                let mut score = -1.0;
                if self.frames.len() >= length
                    && self.levels.iter().skip(offset).sum::<f32>() / length as f32 >= -20.0
                {
                    let mut dot = 0.0f32;
                    let mut energy = 0.0f32;
                    for (f, reference) in self.frames.iter().skip(offset).zip(&fp.frames) {
                        for k in 0..BANDS {
                            dot += f[k] * reference[k];
                            energy += f[k] * f[k];
                        }
                    }
                    score = (dot / (energy.sqrt() + 1e-9)).clamp(-1.0, 1.0);
                }
                scores.push(score);
                if end < self.quiet_until {
                    self.armed[i] = None;
                    continue;
                }
                let onset = end - (length * HOP) as f64 / RATE as f64;
                if score >= self.threshold {
                    if let Some((peak, age)) = &mut self.armed[i] {
                        if score > peak.score {
                            peak.score = score;
                            peak.onset_s = onset;
                            *age = 0;
                        } else {
                            *age += 1;
                        }
                        if *age >= 7 {
                            ready.push(peak.clone());
                        }
                    } else {
                        self.armed[i] = Some((
                            Bite {
                                clip: fp.name.clone(),
                                score,
                                onset_s: onset,
                            },
                            0,
                        ));
                    }
                } else if let Some((peak, _)) = self.armed[i].take() {
                    ready.push(peak);
                }
            }
            if let Some(bite) = ready.into_iter().max_by(|a, b| a.score.total_cmp(&b.score)) {
                result.bites.push(bite);
                self.quiet_until = end + self.refractory;
                self.armed.fill(None);
            }
            result.frames.push(ScoreFrame { index, scores });
        }
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn silence_and_short_audio_do_not_fire() {
        let cancel = Cancel::default();
        let mut detector = Detector::new(0.66, 3.0).unwrap();
        assert!(detector
            .feed(&[0.0; 256], &cancel)
            .unwrap()
            .frames
            .is_empty());
        let result = detector.feed(&vec![0.0; RATE as usize], &cancel).unwrap();
        assert!(result.bites.is_empty());
        assert!(result
            .frames
            .iter()
            .all(|f| f.scores.iter().all(|&s| s == -1.0)));
    }

    #[test]
    fn bundled_clips_match_and_chunk_boundaries_do_not_change_scores() {
        let cancel = Cancel::default();
        let (samples, rate) =
            super::super::decode_wav(std::io::Cursor::new(crate::assets::CLIPS[0].1)).unwrap();
        let mut resampler = super::super::Resampler::new(rate, RATE);
        let mut samples = resampler.feed(&samples);
        samples.extend(resampler.finish());
        samples.extend(vec![0.0; 1024]);
        let mut big = Detector::new(0.66, 3.0).unwrap();
        let expected = big.feed(&samples, &cancel).unwrap();
        let mut small = Detector::new(0.66, 3.0).unwrap();
        let mut actual = Vec::new();
        let mut bites = Vec::new();
        for chunk in samples.chunks(157) {
            let result = small.feed(chunk, &cancel).unwrap();
            actual.extend(result.frames);
            bites.extend(result.bites);
        }
        assert_eq!(actual.len(), expected.frames.len());
        for (a, b) in actual.iter().zip(&expected.frames) {
            assert_eq!(a.scores, b.scores);
        }
        assert!(actual.iter().any(|f| f.scores[0] > 0.999));
        assert_eq!(bites.len(), 1);
        assert_eq!(expected.bites.len(), 1);
    }

    #[test]
    fn cancellation_is_observed_inside_analysis() {
        let cancel = Cancel::default();
        cancel.cancel();
        assert!(Mel::new().features(&[0.0; 1024], &cancel).is_err());
        assert!(Detector::new(0.66, 3.0)
            .unwrap()
            .feed(&[0.0; 512], &cancel)
            .is_err());
    }
}
