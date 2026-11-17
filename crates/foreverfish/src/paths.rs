use anyhow::{Context, Result};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug)]
pub struct Paths {
    pub config: PathBuf,
    pub cache: PathBuf,
    pub logs: PathBuf,
}

impl Paths {
    pub fn discover() -> Result<Self> {
        let paths = if let Some(root) = std::env::var_os("FOREVERFISH_HOME")
            .filter(|s| !s.is_empty())
            .or_else(|| std::env::var_os("WOWFISH_HOME").filter(|s| !s.is_empty()))
        {
            return Self::in_root(PathBuf::from(root));
        } else {
            let base = directories::BaseDirs::new().context("无法定位用户配置目录")?;
            // Match Python/platformdirs so the Rust branch can reuse existing settings.
            #[cfg(windows)]
            let config = base
                .config_local_dir()
                .join("ForeverFishing/ForeverFishing");
            #[cfg(not(windows))]
            let config = base.config_dir().join("ForeverFishing");
            #[cfg(windows)]
            let cache = config.join("Cache");
            #[cfg(not(windows))]
            let cache = base.cache_dir().join("ForeverFishing");
            #[cfg(windows)]
            let logs = config.join("Logs");
            #[cfg(not(windows))]
            let logs = base
                .state_dir()
                .unwrap_or_else(|| base.data_local_dir())
                .join("ForeverFishing/log");
            Self {
                config: config.join("config.toml"),
                cache,
                logs,
            }
        };
        paths.create()
    }

    pub fn in_root(root: impl AsRef<Path>) -> Result<Self> {
        let root = root.as_ref();
        Self {
            config: root.join("config/config.toml"),
            cache: root.join("cache"),
            logs: root.join("log"),
        }
        .create()
    }

    fn create(self) -> Result<Self> {
        for directory in [self.config.parent().unwrap(), &self.cache, &self.logs] {
            std::fs::create_dir_all(directory)
                .with_context(|| format!("无法创建目录 {}", directory.display()))?;
        }
        Ok(self)
    }
}
