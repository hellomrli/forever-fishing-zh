use anyhow::{bail, ensure, Context, Result};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};
use toml_edit::{DocumentMut, Item, Table};

macro_rules! settings {
    ($name:ident { $($field:ident: $ty:ty = $default:expr),* $(,)? }) => {
        #[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
        #[serde(default)]
        pub struct $name { $(pub $field: $ty),* }
        impl Default for $name {
            fn default() -> Self { Self { $($field: $default),* } }
        }
    };
}

settings!(Game {
    cast_key: String = "1".into(),
    interact_key: String = "f".into(),
    session_minutes: u32 = 0,
    quit_key: String = String::new(),
    lure_key: String = String::new(),
    lure_minutes: f64 = 10.0,
    window_title: String = String::new(),
    pause_hotkey: String = "f8".into(),
    stop_hotkey: String = "f9".into(),
});
settings!(Vision {
    icon_size_px: u32 = 0,
    icon_native_size: u32 = 0,
    icon_threshold: f32 = 0.75,
    match_width: u32 = 1920,
    search_left: f32 = 0.0,
    search_top: f32 = 0.0,
    search_right: f32 = 1.0,
    search_bottom: f32 = 1.0,
});
settings!(Audio {
    capture: String = "game".into(),
    device: String = String::new(),
    bite_threshold: f32 = 0.66,
    refractory_s: f64 = 3.0,
    fallback_to_device: bool = true,
});
settings!(Run {
    icon_timeout_s: f64 = 4.0,
    cast_timeout_s: f64 = 30.0,
    pause_min_s: f64 = 0.5,
    pause_max_s: f64 = 2.0,
    key_hold_ms: u32 = 60,
    lure_cast_s: f64 = 6.0,
    tick_interval_s: f64 = 0.02,
});
settings!(Behavior {
    break_every_min_minutes: f64 = 0.0,
    break_every_max_minutes: f64 = 0.0,
    break_min_minutes: f64 = 0.0,
    break_max_minutes: f64 = 0.0,
    key_hold_jitter_ms: u32 = 0,
    jump_chance: f64 = 0.0,
    jump_key: String = "space".into(),
});

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub game: Game,
    pub vision: Vision,
    pub audio: Audio,
    pub run: Run,
    pub behavior: Behavior,
}

pub fn key_code(key: &str) -> Option<u16> {
    let key = key.trim().to_ascii_lowercase();
    if key.len() == 1 {
        let b = key.as_bytes()[0];
        if b.is_ascii_alphanumeric() {
            return Some(b.to_ascii_uppercase() as u16);
        }
    }
    if let Some(n) = key.strip_prefix('f').and_then(|n| n.parse::<u16>().ok()) {
        if (1..=12).contains(&n) {
            return Some(0x6f + n);
        }
    }
    Some(match key.as_str() {
        "space" => 0x20,
        "enter" => 0x0d,
        "tab" => 0x09,
        "escape" => 0x1b,
        "-" => 0xbd,
        "=" => 0xbb,
        "[" => 0xdb,
        "]" => 0xdd,
        ";" => 0xba,
        "'" => 0xde,
        "," => 0xbc,
        "." => 0xbe,
        "/" => 0xbf,
        "`" => 0xc0,
        "\\" => 0xdc,
        _ => return None,
    })
}

impl Config {
    pub fn parse(text: &str) -> Result<Self> {
        let config: Self = toml::from_str(text).context("配置格式错误，请检查 UTF-8 TOML 文件")?;
        config.validate()?;
        Ok(config)
    }

    pub fn validate(&self) -> Result<()> {
        fn finite(value: &toml::Value) -> bool {
            match value {
                toml::Value::Float(v) => v.is_finite(),
                toml::Value::Table(t) => t.values().all(finite),
                _ => true,
            }
        }
        ensure!(
            finite(&toml::Value::try_from(self)?),
            "配置数值必须有限，不能是 nan 或 inf"
        );
        let (g, v, a, r, b) = (
            &self.game,
            &self.vision,
            &self.audio,
            &self.run,
            &self.behavior,
        );
        ensure!(
            key_code(&g.cast_key).is_some(),
            "抛竿键不能为空，且必须是支持的单键"
        );
        ensure!(
            key_code(&g.interact_key).is_some(),
            "互动键不能为空，且必须是支持的单键"
        );
        for key in [&g.quit_key, &g.lure_key] {
            ensure!(
                key.trim().is_empty() || key_code(key).is_some(),
                "宏键必须是支持的单键或留空"
            );
        }
        let hotkeys: Vec<_> = [&g.pause_hotkey, &g.stop_hotkey]
            .into_iter()
            .filter(|k| !k.trim().is_empty())
            .map(|k| key_code(k))
            .collect();
        ensure!(
            hotkeys.iter().all(|k| matches!(k, Some(0x70..=0x7b))),
            "快捷键仅支持 F1–F12 或留空"
        );
        ensure!(
            hotkeys.len() < 2 || hotkeys[0] != hotkeys[1],
            "暂停与停止快捷键不能相同"
        );
        for key in [&g.cast_key, &g.interact_key, &g.quit_key, &g.lure_key] {
            ensure!(
                key.trim().is_empty() || !hotkeys.contains(&key_code(key)),
                "快捷键不能与游戏操作键冲突"
            );
        }
        ensure!(g.lure_minutes > 0.0, "补饵周期必须大于 0");
        ensure!(
            v.icon_threshold > 0.0 && v.icon_threshold <= 1.0,
            "图标阈值必须在 (0, 1] 内"
        );
        ensure!(
            v.match_width >= 320 && v.match_width <= 8192,
            "识别宽度必须在 320–8192 内"
        );
        ensure!(v.icon_size_px <= 512, "图标尺寸必须在 0–512 内");
        ensure!(
            0.0 <= v.search_left
                && v.search_left < v.search_right
                && v.search_right <= 1.0
                && 0.0 <= v.search_top
                && v.search_top < v.search_bottom
                && v.search_bottom <= 1.0,
            "水面范围必须在 0–1 内，且左上角在右下角之前"
        );
        ensure!(
            a.capture == "game" || a.capture == "device",
            "声音来源必须是 game 或 device"
        );
        ensure!(
            a.bite_threshold > 0.0 && a.bite_threshold <= 1.0,
            "水花阈值必须在 (0, 1] 内"
        );
        ensure!(a.refractory_s >= 0.0, "水花间隔不能小于 0");
        ensure!(r.icon_timeout_s > 0.0, "抛竿后等待齿轮出现的时间必须大于 0");
        ensure!(r.cast_timeout_s > 0.0, "无咬钩声音重抛时间必须大于 0");
        ensure!(
            r.pause_min_s >= 0.0 && r.pause_min_s <= r.pause_max_s,
            "抛竿间隔范围无效"
        );
        ensure!(r.lure_cast_s >= 0.0, "补饵等待不能小于 0");
        ensure!(
            (0.005..=0.2).contains(&r.tick_interval_s),
            "状态检查间隔必须在 0.005–0.2 秒内"
        );
        ensure!(
            (10..=500).contains(&r.key_hold_ms),
            "按键时长必须在 10–500 毫秒内"
        );
        for (lo, hi) in [
            (b.break_every_min_minutes, b.break_every_max_minutes),
            (b.break_min_minutes, b.break_max_minutes),
        ] {
            ensure!(lo >= 0.0 && lo <= hi, "可选行为的时间范围无效");
        }
        ensure!((0.0..=1.0).contains(&b.jump_chance), "可选行为概率无效");
        ensure!(
            b.key_hold_jitter_ms <= r.key_hold_ms - 10,
            "按键抖动必须保留至少 10 毫秒"
        );
        ensure!(key_code(&b.jump_key).is_some(), "跳跃键无效");
        if b.jump_chance > 0.0 {
            ensure!(
                !hotkeys.contains(&key_code(&b.jump_key)),
                "跳跃键不能与全局快捷键冲突"
            );
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub struct Store {
    pub path: PathBuf,
}

impl Store {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    fn lock(&self) -> Result<File> {
        fs::create_dir_all(parent_directory(&self.path))?;
        let mut name = self.path.as_os_str().to_os_string();
        name.push(".lock");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(name)?;
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            match file.try_lock_exclusive() {
                Ok(()) => return Ok(file),
                // Windows lock contention uses ERROR_LOCK_VIOLATION rather than WouldBlock.
                Err(error)
                    if error.raw_os_error() == fs2::lock_contended_error().raw_os_error() =>
                {
                    if Instant::now() >= deadline {
                        bail!("配置文件被其他任务占用，请稍后重试");
                    }
                    thread::sleep(Duration::from_millis(20));
                }
                Err(error) => return Err(error.into()),
            }
        }
    }

    fn text(&self) -> Result<String> {
        if self.path.exists() {
            fs::read_to_string(&self.path)
                .with_context(|| format!("无法读取 UTF-8 配置 {}", self.path.display()))
        } else {
            Ok(crate::assets::DEFAULT_CONFIG.into())
        }
    }

    pub fn load(&self) -> Result<Config> {
        Config::parse(&self.text()?).with_context(|| format!("配置文件：{}", self.path.display()))
    }

    pub fn ensure(&self) -> Result<()> {
        let _lock = self.lock()?;
        if !self.path.exists() {
            atomic_write(&self.path, crate::assets::DEFAULT_CONFIG)?;
        }
        Ok(())
    }

    /// Read the latest file inside one process-wide/cross-process filesystem lock.
    /// Callers modify just their section, preserving newer calibration and unknown fields.
    pub fn update(&self, change: impl FnOnce(&mut Config)) -> Result<Config> {
        let _lock = self.lock()?;
        let text = self.text()?;
        let mut config = Config::parse(&text)?;
        let previous = toml::to_string(&config)?.parse::<DocumentMut>()?;
        change(&mut config);
        config.validate()?;
        let mut document = text.parse::<DocumentMut>()?;
        let replacement = toml::to_string(&config)?.parse::<DocumentMut>()?;
        for (section, item) in replacement.iter() {
            let table = item.as_table().context("配置区段必须为表格")?;
            for (key, new) in table.iter() {
                // Compare typed values so untouched fields retain their original quotes,
                // numeric formatting and comments, including omitted default fields.
                if previous[section][key].to_string() == new.to_string() {
                    continue;
                }
                let target = document
                    .entry(section)
                    .or_insert_with(|| Item::Table(Table::new()))
                    .as_table_like_mut()
                    .context("配置区段必须为表格")?;
                if let (Some(old), Some(new_value)) =
                    (target.get(key).and_then(Item::as_value), new.as_value())
                {
                    let mut changed = new_value.clone();
                    *changed.decor_mut() = old.decor().clone();
                    target.insert(key, Item::Value(changed));
                } else {
                    target.insert(key, new.clone());
                }
            }
        }
        atomic_write(&self.path, &document.to_string())?;
        Ok(config)
    }

    pub fn recover(&self) -> Result<PathBuf> {
        let _lock = self.lock()?;
        let mut backup = self.path.with_extension("toml.bak");
        let mut index = 0;
        while backup.exists() {
            index += 1;
            backup = self.path.with_extension(format!("toml.bak.{index}"));
        }
        if self.path.exists() {
            fs::copy(&self.path, &backup)?;
        }
        atomic_write(&self.path, crate::assets::DEFAULT_CONFIG)?;
        Ok(backup)
    }
}

pub fn atomic_write(path: &Path, text: &str) -> Result<()> {
    let parent = parent_directory(path);
    fs::create_dir_all(parent)?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    file.write_all(text.as_bytes())?;
    file.as_file().sync_all()?;
    file.persist(path).map_err(|error| error.error)?;
    #[cfg(unix)]
    File::open(parent)?.sync_all()?;
    Ok(())
}

fn parent_directory(path: &Path) -> &Path {
    path.parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_python_template() {
        assert_eq!(
            Config::parse(crate::assets::DEFAULT_CONFIG).unwrap(),
            Config::default()
        );
    }

    #[test]
    fn rejects_invalid_keys_conflicts_nan_and_region() {
        for text in [
            "[game]\ncast_key=''",
            "[game]\npause_hotkey='1'",
            "[game]\ncast_key='F8'",
            "[audio]\nbite_threshold=nan",
            "[vision]\nsearch_left=0.8\nsearch_right=0.5",
            "[run]\nicon_timeout_s=0.0",
            "[behavior]\nbreak_min_minutes=100\nbreak_max_minutes=10",
        ] {
            assert!(Config::parse(text).is_err(), "{text}");
        }
    }

    #[test]
    fn preserves_comments_unknown_fields_and_latest_calibration() {
        let directory = tempfile::tempdir().unwrap();
        let store = Store::new(directory.path().join("中文配置.toml"));
        fs::write(&store.path, "# 中文注释\n[game]\ncast_key='1' # 保留\ncustom=42\n[audio]\ndevice='扬声器'\n[custom]\nhello='你好'\n").unwrap();
        store.update(|c| c.vision.icon_size_px = 26).unwrap();
        let calibrated = fs::read_to_string(&store.path).unwrap();
        assert!(calibrated.contains("cast_key='1' # 保留"));
        assert!(calibrated.contains("device='扬声器'"));
        assert!(!calibrated.contains("[behavior]"));
        store.update(|c| c.game.cast_key = "2".into()).unwrap();
        let result = fs::read_to_string(&store.path).unwrap();
        assert!(result.contains("# 中文注释") && result.contains("# 保留"));
        assert!(result.contains("custom=42") && result.contains("hello='你好'"));
        assert_eq!(store.load().unwrap().vision.icon_size_px, 26);
        assert_eq!(store.load().unwrap().audio.device, "扬声器");
    }

    #[test]
    fn updates_inline_tables_without_losing_unknown_values() {
        let directory = tempfile::tempdir().unwrap();
        let store = Store::new(directory.path().join("config.toml"));
        fs::write(
            &store.path,
            "game = { cast_key = '1', custom = '保留' } # 行内表\n",
        )
        .unwrap();
        store.update(|c| c.game.session_minutes = 30).unwrap();
        let text = fs::read_to_string(&store.path).unwrap();
        assert!(text.contains("cast_key = '1'"));
        assert!(text.contains("custom = '保留'"));
        assert!(text.contains("# 行内表"));
        assert_eq!(store.load().unwrap().game.session_minutes, 30);
    }

    #[test]
    fn concurrent_updates_do_not_overwrite_other_sections() {
        let directory = tempfile::tempdir().unwrap();
        let store = Store::new(directory.path().join("config.toml"));
        store.ensure().unwrap();
        let threads: Vec<_> = (0..8)
            .map(|_| {
                let store = store.clone();
                thread::spawn(move || {
                    for _ in 0..10 {
                        store.update(|c| c.game.session_minutes += 1).unwrap();
                    }
                })
            })
            .collect();
        for thread in threads {
            thread.join().unwrap();
        }
        assert_eq!(store.load().unwrap().game.session_minutes, 80);
    }

    #[test]
    fn bad_utf8_is_backed_up_and_failed_validation_keeps_file() {
        let directory = tempfile::tempdir().unwrap();
        let store = Store::new(directory.path().join("config.toml"));
        fs::write(&store.path, [0xff, 0xfe]).unwrap();
        assert!(store.load().is_err());
        let backup = store.recover().unwrap();
        assert_eq!(fs::read(backup).unwrap(), [0xff, 0xfe]);
        let original = fs::read(&store.path).unwrap();
        assert!(store.update(|c| c.audio.bite_threshold = -1.0).is_err());
        assert_eq!(fs::read(&store.path).unwrap(), original);
    }
}
