//! Golden values come from the Python implementation, not from Rust's own output.
use foreverfish::{
    assets,
    audio::{self, Bite, Cancel, Fingerprint, Mel, BANDS, FFT_SIZE, HOP, RATE},
    config::Config,
};
use serde::Deserialize;

#[derive(Deserialize)]
struct Fixture {
    sample_rate: u32,
    fft_size: usize,
    hop: usize,
    bands: usize,
    probes: Vec<Probe>,
    recordings: Vec<Recording>,
}

#[derive(Deserialize)]
struct Probe {
    clip: String,
    frames: Vec<Frame>,
    fingerprint_length: usize,
    fingerprint_probes: Vec<Value>,
}

#[derive(Deserialize)]
struct Frame {
    index: usize,
    bands: Vec<f32>,
}

#[derive(Deserialize)]
struct Value {
    frame: usize,
    band: usize,
    value: f32,
}

#[derive(Deserialize)]
struct Recording {
    clip: String,
    padding: usize,
    duration_s: f64,
    peaks: Vec<ExpectedBite>,
    bites: Vec<ExpectedBite>,
    floor: f32,
    threshold: f32,
}

#[derive(Deserialize)]
struct ExpectedBite {
    clip: String,
    score: f32,
    onset_s: f64,
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!("fixtures/python_audio.json")).unwrap()
}

fn clip(name: &str) -> Vec<f32> {
    let (_, bytes) = assets::CLIPS
        .iter()
        .find(|(clip, _)| *clip == name)
        .unwrap();
    let (samples, rate) = audio::decode_wav(std::io::Cursor::new(*bytes)).unwrap();
    assert_eq!(rate, RATE);
    samples
}

fn compare_bites(actual: &[Bite], expected: &[ExpectedBite]) {
    assert_eq!(actual.len(), expected.len());
    for (actual, expected) in actual.iter().zip(expected) {
        assert_eq!(actual.clip, expected.clip);
        assert!(
            (actual.score - expected.score).abs() < 2e-5,
            "{}: Rust {} vs Python {}",
            actual.clip,
            actual.score,
            expected.score
        );
        assert!(
            (actual.onset_s - expected.onset_s).abs() <= HOP as f64 / RATE as f64,
            "{}: Rust onset {} vs Python {}",
            actual.clip,
            actual.onset_s,
            expected.onset_s
        );
    }
}

#[test]
fn mel_features_and_fingerprints_match_python_reference() {
    let fixture = fixture();
    assert_eq!(fixture.sample_rate, RATE);
    assert_eq!(fixture.fft_size, FFT_SIZE);
    assert_eq!(fixture.hop, HOP);
    assert_eq!(fixture.bands, BANDS);
    let fingerprints = Fingerprint::bundled().unwrap();
    for probe in fixture.probes {
        let frames = Mel::new()
            .features(&clip(&probe.clip), &Cancel::default())
            .unwrap();
        for frame in probe.frames {
            for (band, &expected) in frame.bands.iter().enumerate() {
                assert!(
                    (frames[frame.index][band] - expected).abs() < 2e-3,
                    "{} frame {} band {}: Rust {} vs Python {}",
                    probe.clip,
                    frame.index,
                    band,
                    frames[frame.index][band],
                    expected
                );
            }
        }
        let fp = fingerprints
            .iter()
            .find(|fp| fp.name == probe.clip)
            .unwrap();
        assert_eq!(fp.frames.len(), probe.fingerprint_length);
        for value in probe.fingerprint_probes {
            assert!((fp.frames[value.frame][value.band] - value.value).abs() < 2e-6);
        }
    }
}

#[test]
fn all_splash_reports_match_python_with_unaligned_onsets() {
    for expected in fixture().recordings {
        let mut samples = vec![0.0; expected.padding];
        samples.extend(clip(&expected.clip));
        samples.extend(vec![0.0; RATE as usize * 2]);
        let actual = audio::analyze_samples(&samples, RATE, &Cancel::default()).unwrap();
        assert!((actual.duration_s - expected.duration_s).abs() < 1e-9);
        compare_bites(&actual.peaks, &expected.peaks);
        compare_bites(&actual.bites, &expected.bites);
        assert!((actual.floor - expected.floor).abs() < 2e-5);
        assert!((actual.threshold - expected.threshold).abs() < 1e-6);
    }
}

#[test]
fn embedded_configuration_stays_compatible_with_python_defaults() {
    let python = Config::parse(include_str!("../../../src/wowfish/assets/default_config")).unwrap();
    assert_eq!(python, Config::parse(assets::DEFAULT_CONFIG).unwrap());
}
