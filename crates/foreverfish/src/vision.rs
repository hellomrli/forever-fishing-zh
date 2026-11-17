use crate::{assets, audio::Cancel, config};
use anyhow::{ensure, Result};
use image::{
    imageops::{self, FilterType},
    GrayImage, RgbImage,
};
use serde::Serialize;
use std::{
    cmp::{Ordering, Reverse},
    collections::{BTreeMap, BinaryHeap, VecDeque},
};

pub type Frame = RgbImage;
pub const GOLD_MIN: f32 = 0.12;
pub const PROBES: &[u32] = &[12, 14, 16, 18, 20, 23, 26, 30, 35, 42, 50, 60, 72, 88, 104];

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct Region {
    pub left: f32,
    pub top: f32,
    pub right: f32,
    pub bottom: f32,
}

impl Default for Region {
    fn default() -> Self {
        Self {
            left: 0.0,
            top: 0.0,
            right: 1.0,
            bottom: 1.0,
        }
    }
}

impl Region {
    pub fn from_config(v: &config::Vision) -> Self {
        Self {
            left: v.search_left,
            top: v.search_top,
            right: v.search_right,
            bottom: v.search_bottom,
        }
    }
    pub fn around(x: u32, y: u32, width: u32, height: u32, radius: u32) -> Self {
        Self {
            left: x.saturating_sub(radius) as f32 / width as f32,
            top: y.saturating_sub(radius) as f32 / height as f32,
            right: (x + radius).min(width) as f32 / width as f32,
            bottom: (y + radius).min(height) as f32 / height as f32,
        }
    }
    pub fn intersection(self, other: Self) -> Self {
        Self {
            left: self.left.max(other.left),
            top: self.top.max(other.top),
            right: self.right.min(other.right),
            bottom: self.bottom.min(other.bottom),
        }
    }
    pub fn pixels(self, w: u32, h: u32) -> (u32, u32, u32, u32) {
        let x0 = (self.left.clamp(0.0, 1.0) * w as f32).floor() as u32;
        let y0 = (self.top.clamp(0.0, 1.0) * h as f32).floor() as u32;
        let x1 = (self.right.clamp(0.0, 1.0) * w as f32).ceil() as u32;
        let y1 = (self.bottom.clamp(0.0, 1.0) * h as f32).ceil() as u32;
        (x0.min(w), y0.min(h), x1.min(w), y1.min(h))
    }
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct IconMatch {
    pub score: f32,
    pub x: u32,
    pub y: u32,
    pub size: u32,
    pub gold: f32,
}

impl IconMatch {
    pub fn none(size: u32) -> Self {
        Self {
            score: -1.0,
            x: 0,
            y: 0,
            size,
            gold: 0.0,
        }
    }
    pub fn accepted(self, threshold: f32) -> bool {
        self.score >= threshold && self.gold >= GOLD_MIN
    }
}

#[derive(Clone)]
struct Pixels {
    values: Vec<(u32, u32, [f32; 3])>,
    mean: [f32; 3],
    energy: f32,
}

impl Pixels {
    fn new(values: Vec<(u32, u32, [f32; 3])>) -> Self {
        let mut mean = [0.0; 3];
        for (_, _, color) in &values {
            for k in 0..3 {
                mean[k] += color[k];
            }
        }
        for mean in &mut mean {
            *mean /= values.len().max(1) as f32;
        }
        let energy = values
            .iter()
            .flat_map(|(_, _, c)| (0..3).map(move |k| (c[k] - mean[k]).powi(2)))
            .sum();
        Self {
            values,
            mean,
            energy,
        }
    }
    fn score(&self, frame: &Frame, x: u32, y: u32) -> (f32, f32) {
        let mut sum = [0.0; 3];
        let mut square = 0.0;
        let mut dot = 0.0;
        let bytes = frame.as_raw();
        let width = frame.width();
        for (tx, ty, reference) in &self.values {
            let offset = (((y + ty) * width + x + tx) * 3) as usize;
            for k in 0..3 {
                let value = bytes[offset + k] as f32;
                sum[k] += value;
                square += value * value;
                dot += value * (reference[k] - self.mean[k]);
            }
        }
        let n = self.values.len().max(1) as f32;
        let variance = (square - sum.iter().map(|v| v * v / n).sum::<f32>()).max(0.0);
        let norm = (variance * self.energy).sqrt();
        let score = if norm < 1e-6 {
            -1.0
        } else {
            (dot / norm).clamp(-1.0, 1.0)
        };
        (score, (sum[0] - sum[2]) / (255.0 * n))
    }
    fn warm(&self, frame: &Frame, x: u32, y: u32) -> bool {
        let bytes = frame.as_raw();
        let mut rb = 0.0;
        for (tx, ty, _) in self
            .values
            .iter()
            .step_by((self.values.len() / 6).max(1))
            .take(6)
        {
            let i = (((y + ty) * frame.width() + x + tx) * 3) as usize;
            rb += bytes[i] as f32 - bytes[i + 2] as f32;
        }
        rb >= 6.0 * 255.0 * 0.06
    }
}

#[derive(Clone)]
struct Template {
    native: u32,
    full: Pixels,
    sparse: Pixels,
}

#[derive(Clone, Copy, Debug)]
struct Candidate {
    score: f32,
    x: u32,
    y: u32,
}
impl PartialEq for Candidate {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other).is_eq()
    }
}
impl Eq for Candidate {}
impl PartialOrd for Candidate {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Candidate {
    fn cmp(&self, other: &Self) -> Ordering {
        self.score
            .total_cmp(&other.score)
            .then(self.x.cmp(&other.x))
            .then(self.y.cmp(&other.y))
    }
}

pub struct Matcher {
    natives: Vec<(u32, Frame, GrayImage)>,
    templates: BTreeMap<u32, Template>,
    pub match_width: u32,
}

impl Matcher {
    pub fn new(match_width: u32) -> Result<Self> {
        let mut natives = Vec::new();
        for &(size, bytes) in assets::ICONS {
            let rgba = image::load_from_memory(bytes)?.into_rgba8();
            let rgb = image::DynamicImage::ImageRgba8(rgba.clone()).into_rgb8();
            let mask = GrayImage::from_fn(size, size, |x, y| {
                image::Luma([if rgba.get_pixel(x, y)[3] == 255 {
                    255
                } else {
                    0
                }])
            });
            natives.push((size, rgb, mask));
        }
        Ok(Self {
            natives,
            templates: BTreeMap::new(),
            match_width,
        })
    }

    fn template(&mut self, size: u32) -> &Template {
        self.templates.entry(size).or_insert_with(|| {
            let (native, rgb, mask) = self
                .natives
                .iter()
                .find(|(n, _, _)| *n >= size)
                .unwrap_or(self.natives.last().unwrap());
            let rgb = imageops::resize(rgb, size, size, FilterType::Lanczos3);
            let mask = imageops::resize(mask, size, size, FilterType::Nearest);
            let mut values = Vec::new();
            for y in 0..size {
                for x in 0..size {
                    if mask.get_pixel(x, y)[0] > 0 {
                        let c = rgb.get_pixel(x, y);
                        values.push((x, y, [c[0] as f32, c[1] as f32, c[2] as f32]));
                    }
                }
            }
            let samples = (0..48.min(values.len()))
                .map(|i| values[i * values.len() / 48.min(values.len())])
                .collect();
            Template {
                native: *native,
                full: Pixels::new(values),
                sparse: Pixels::new(samples),
            }
        })
    }

    /// Sparse candidates are verified with every fully opaque template pixel.
    /// Work stays bounded; cancellation is checked on every few scan lines.
    pub fn find(
        &mut self,
        frame: &Frame,
        size: u32,
        region: Region,
        cancel: &Cancel,
    ) -> Result<IconMatch> {
        cancel.check()?;
        ensure!(size > 0 && size <= 512, "无效图标尺寸");
        let (w, h) = frame.dimensions();
        if w == 0 || h == 0 {
            return Ok(IconMatch::none(size));
        }
        let (x0, y0, x1, y1) = region.pixels(w, h);
        if x1 <= x0 || y1 <= y0 {
            return Ok(IconMatch::none(size));
        }
        let scale = 1.0f32.min((self.match_width as f32 / w as f32).max(12.0 / size as f32));
        let side = ((size as f32 * scale).round() as u32).max(1);
        if side < 12 {
            return Ok(IconMatch::none(size));
        }
        let crop = imageops::crop_imm(frame, x0, y0, x1 - x0, y1 - y0).to_image();
        let crop = if scale < 1.0 {
            imageops::resize(
                &crop,
                ((crop.width() as f32 * scale).round() as u32).max(1),
                ((crop.height() as f32 * scale).round() as u32).max(1),
                FilterType::Triangle,
            )
        } else {
            crop
        };
        if crop.width() < side || crop.height() < side {
            return Ok(IconMatch::none(size));
        }
        let template = self.template(side);
        let mut candidates = BinaryHeap::new();
        let stride = if crop.width() > 1000 && side >= 24 {
            2usize
        } else {
            1
        };
        for y in (0..=crop.height() - side).step_by(stride) {
            if y % 16 == 0 {
                cancel.check()?;
            }
            for x in (0..=crop.width() - side).step_by(stride) {
                if !template.sparse.warm(&crop, x, y) {
                    continue;
                }
                let (score, gold) = template.sparse.score(&crop, x, y);
                if score < 0.35 || gold < 0.08 {
                    continue;
                }
                let candidate = Candidate { score, x, y };
                if candidates.len() < 128 {
                    candidates.push(Reverse(candidate));
                } else if candidates.peek().is_some_and(|c| candidate > c.0) {
                    candidates.pop();
                    candidates.push(Reverse(candidate));
                }
            }
        }
        let mut best = IconMatch::none(size);
        for Reverse(candidate) in candidates {
            cancel.check()?;
            let radius = stride as u32 - 1;
            for y in candidate.y.saturating_sub(radius)
                ..=(candidate.y + radius).min(crop.height() - side)
            {
                for x in candidate.x.saturating_sub(radius)
                    ..=(candidate.x + radius).min(crop.width() - side)
                {
                    let (score, gold) = template.full.score(&crop, x, y);
                    if gold >= GOLD_MIN && score > best.score {
                        best = IconMatch {
                            score,
                            gold,
                            size,
                            x: x0 + ((x as f32 + side as f32 / 2.0) / scale).round() as u32,
                            y: y0 + ((y as f32 + side as f32 / 2.0) / scale).round() as u32,
                        };
                    }
                }
            }
        }
        Ok(best)
    }

    pub fn calibrate(
        &mut self,
        frame: &Frame,
        region: Region,
        cancel: &Cancel,
    ) -> Result<Calibration> {
        let (w, h) = frame.dimensions();
        let mut probes = Vec::new();
        for &side in PROBES {
            probes.push(self.find(frame, side, region, cancel)?);
        }
        probes.sort_by(|a, b| b.score.total_cmp(&a.score));
        let mut anchors: Vec<IconMatch> = Vec::new();
        for probe in probes {
            if probe.score >= 0.0
                && anchors
                    .iter()
                    .all(|a| a.x.abs_diff(probe.x) > 128 || a.y.abs_diff(probe.y) > 128)
            {
                anchors.push(probe);
            }
            if anchors.len() == 3 {
                break;
            }
        }
        let mut best = IconMatch::none(12);
        let mut rank = -2.0f32;
        for anchor in anchors {
            for side in 12..128 {
                let near =
                    Region::around(anchor.x, anchor.y, w, h, side / 2 + 24).intersection(region);
                let found = self.find(frame, side, near, cancel)?;
                let key = found.score - 1.0 / side as f32;
                if found.gold >= GOLD_MIN && key > rank {
                    best = found;
                    rank = key;
                }
            }
        }
        let mut off = -1.0f32;
        for factor in [0.8, 1.25] {
            let side = (best.size as f32 * factor).round().max(1.0) as u32;
            let near = Region::around(best.x, best.y, w, h, side / 2 + 24).intersection(region);
            off = off.max(self.find(frame, side, near, cancel)?.score);
        }
        let native_size = self.template(best.size).native;
        Ok(Calibration {
            icon: best,
            native_size,
            prominence: best.score - off,
        })
    }
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct Calibration {
    pub icon: IconMatch,
    pub native_size: u32,
    pub prominence: f32,
}

impl Calibration {
    pub fn valid(self) -> bool {
        self.icon.accepted(0.75) && self.prominence >= 0.25
    }
    pub fn threshold(self) -> f32 {
        ((self.icon.score - 0.1) * 100.0).round().clamp(60.0, 90.0) / 100.0
    }
}

pub struct Motion {
    threshold: f32,
    previous: Option<GrayImage>,
    baseline: VecDeque<f32>,
    changes: VecDeque<(f64, f32, f32)>,
}

impl Motion {
    pub fn new(threshold: f32) -> Self {
        Self {
            threshold,
            previous: None,
            baseline: VecDeque::new(),
            changes: VecDeque::new(),
        }
    }
    pub fn reset(&mut self) {
        self.previous = None;
        self.baseline.clear();
        self.changes.clear();
    }
    pub fn observe(&mut self, frame: &Frame, now: f64) -> f32 {
        let gray = image::DynamicImage::ImageRgb8(frame.clone()).into_luma8();
        let gray = imageops::resize(&gray, 64, 64, FilterType::Triangle);
        let Some(previous) = self.previous.replace(gray.clone()) else {
            return 0.0;
        };
        let mut changes: Vec<f32> = gray
            .as_raw()
            .iter()
            .zip(previous.as_raw())
            .map(|(a, b)| a.abs_diff(*b) as f32)
            .collect();
        let at = (changes.len() as f32 * 0.95) as usize;
        changes.select_nth_unstable_by(at, f32::total_cmp);
        let change = changes[at];
        let mut baseline: Vec<_> = self.baseline.iter().copied().collect();
        baseline.sort_by(f32::total_cmp);
        let background = baseline.get(baseline.len() / 2).copied().unwrap_or(0.0);
        self.changes
            .push_back((now, change, self.threshold.max(background * 2.5 + 3.0)));
        self.baseline.push_back(change);
        if self.baseline.len() > 30 {
            self.baseline.pop_front();
        }
        while self.changes.front().is_some_and(|(t, _, _)| now - t > 1.5) {
            self.changes.pop_front();
        }
        change
    }
    pub fn confirmed(&self, now: f64) -> bool {
        self.changes
            .iter()
            .any(|(t, value, bar)| now >= *t && now - t <= 1.5 && value >= bar)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    pub fn scene(size: u32, x: u32, y: u32) -> Frame {
        let mut matcher = Matcher::new(1920).unwrap();
        let template = matcher.template(size);
        let mut frame = Frame::from_pixel(320, 240, image::Rgb([30, 70, 110]));
        for &(tx, ty, c) in &template.full.values {
            frame.put_pixel(
                x + tx,
                y + ty,
                image::Rgb([c[0] as u8, c[1] as u8, c[2] as u8]),
            );
        }
        frame
    }

    #[test]
    fn matches_opaque_gold_at_small_and_native_sizes() {
        let mut matcher = Matcher::new(1920).unwrap();
        for size in [12, 19, 29, 42, 79] {
            let frame = scene(size, 80, 60);
            let found = matcher
                .find(&frame, size, Region::default(), &Cancel::default())
                .unwrap();
            assert!(found.accepted(0.99), "{size}: {found:?}");
            assert!(found.x.abs_diff(80 + size / 2) <= 1 && found.y.abs_diff(60 + size / 2) <= 1);
        }
    }

    #[test]
    fn rejects_grey_and_respects_search_rectangle() {
        let mut matcher = Matcher::new(1920).unwrap();
        let frame = scene(29, 80, 60);
        let restricted = Region {
            left: 0.6,
            ..Region::default()
        };
        assert!(!matcher
            .find(&frame, 29, restricted, &Cancel::default())
            .unwrap()
            .accepted(0.75));
        let grey = Frame::from_fn(frame.width(), frame.height(), |x, y| {
            let c = frame.get_pixel(x, y);
            let mean = (c[0] as u32 + c[1] as u32 + c[2] as u32) / 3;
            image::Rgb([mean as u8; 3])
        });
        assert!(!matcher
            .find(&grey, 29, Region::default(), &Cancel::default())
            .unwrap()
            .accepted(0.75));
    }

    #[test]
    fn calibration_requires_size_prominence_and_can_cancel() {
        let mut matcher = Matcher::new(1920).unwrap();
        let result = matcher
            .calibrate(&scene(26, 90, 65), Region::default(), &Cancel::default())
            .unwrap();
        assert!(result.valid(), "{result:?}");
        assert_eq!(result.icon.size, 26);
        let cancel = Cancel::default();
        cancel.cancel();
        assert!(matcher
            .calibrate(&scene(26, 90, 65), Region::default(), &cancel)
            .is_err());
    }

    #[test]
    fn motion_uses_local_changes_and_expires() {
        let mut motion = Motion::new(8.0);
        let a = Frame::from_pixel(64, 64, image::Rgb([80; 3]));
        motion.observe(&a, 0.0);
        motion.observe(&a, 0.1);
        assert!(!motion.confirmed(0.1));
        let b = Frame::from_fn(64, 64, |x, y| {
            if x < 24 && y < 24 {
                image::Rgb([180; 3])
            } else {
                image::Rgb([80; 3])
            }
        });
        motion.observe(&b, 0.2);
        assert!(motion.confirmed(0.2));
        assert!(!motion.confirmed(2.0));
    }
}
