use crate::{
    audio::{Cancel, Capture, Device},
    config,
    vision::{Frame, Region},
};
use anyhow::Result;
use serde::Serialize;

#[cfg(windows)]
mod wasapi;
#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use wasapi::{devices, open_audio};
#[cfg(windows)]
pub use windows::*;

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Window {
    pub handle: usize,
    pub pid: u32,
    pub title: String,
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    pub minimized: bool,
}

impl Window {
    pub fn label(&self) -> String {
        format!(
            "{} · PID {} · {}×{}",
            self.title, self.pid, self.width, self.height
        )
    }
}

pub fn is_game_title(title: &str, custom: &str) -> bool {
    if !custom.trim().is_empty() {
        return title.to_lowercase().contains(&custom.trim().to_lowercase());
    }
    let lower = title.to_lowercase();
    lower.contains("world of warcraft") || lower.contains("魔兽世界") || lower.contains("forever")
}

#[cfg(any(windows, test))]
fn is_client_identity(class: &str, executable: &str, custom_title: &str) -> bool {
    let class = class.to_ascii_lowercase();
    let executable = executable.to_ascii_lowercase();
    if class.is_empty()
        || executable.is_empty()
        || class.starts_with("chrome_widgetwin_")
        || class == "mozillawindowclass"
        || executable.starts_with("foreverfishing")
        || matches!(
            executable.as_str(),
            "chrome.exe" | "msedge.exe" | "firefox.exe" | "brave.exe" | "opera.exe" | "vivaldi.exe"
        )
    {
        return false;
    }
    !custom_title.trim().is_empty()
        || class == "gxwindowclass"
        || matches!(
            executable.as_str(),
            "wow.exe"
                | "wow-64.exe"
                | "wowclassic.exe"
                | "wowclassic_t.exe"
                | "wowt.exe"
                | "wowb.exe"
                | "wowclassicb.exe"
        )
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Hotkey {
    Pause,
    Stop,
}

#[cfg(not(windows))]
mod unsupported {
    use super::*;
    fn error() -> anyhow::Error {
        anyhow::anyhow!(
            "Rust 版游戏捕获与输入当前支持 Windows 10 / 11；此平台可使用界面、图像校准和 WAV 分析"
        )
    }
    pub fn windows(_title: &str) -> Result<Vec<Window>> {
        Ok(vec![])
    }
    pub fn refresh(_window: &Window) -> Result<Option<Window>> {
        Err(error())
    }
    pub fn focused(_window: &Window) -> bool {
        false
    }
    pub fn capture(_window: &Window, _region: Region) -> Result<Frame> {
        Err(error())
    }
    pub fn press(_window: &Window, _key: &str, _hold_ms: u32, _cancel: &Cancel) -> Result<bool> {
        Err(error())
    }
    pub fn devices() -> Result<Vec<Device>> {
        Ok(vec![])
    }
    pub fn open_audio(
        _audio: &config::Audio,
        _pid: Option<u32>,
        _cancel: &Cancel,
    ) -> Result<Box<dyn Capture>> {
        Err(error())
    }
    pub fn show_error(_text: &str) {}
    pub fn native_smoke() -> Result<serde_json::Value> {
        Ok(serde_json::json!({ "supported": false }))
    }
    pub struct Hotkeys;
    impl Hotkeys {
        pub fn new(_game: &config::Game) -> Result<Self> {
            Ok(Self)
        }
        pub fn poll(&self) -> Vec<Hotkey> {
            vec![]
        }
    }
}
#[cfg(not(windows))]
pub use unsupported::*;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recognizes_chinese_english_and_custom_client_titles() {
        for title in ["魔兽世界", "World of Warcraft", "Forever · 客户端二"] {
            assert!(is_game_title(title, ""));
        }
        assert!(!is_game_title("记事本", ""));
        assert!(is_game_title("客户测试窗口", "测试"));
    }

    #[test]
    fn title_matches_need_a_client_identity_and_exclude_browsers_and_helpers() {
        for custom in ["", "Forever"] {
            for (class, executable) in [
                ("Chrome_WidgetWin_1", "msedge.exe"),
                ("Chrome_WidgetWin_1", "renamed.exe"),
                ("MozillaWindowClass", "browser.exe"),
                ("custom", "CHROME.EXE"),
                ("TkTopLevel", "ForeverFishing.exe"),
                ("GxWindowClass", ""),
                ("", "Wow.exe"),
            ] {
                assert!(!is_client_identity(class, executable, custom));
            }
        }
        assert!(!is_client_identity("Notepad", "notepad.exe", ""));
        assert!(is_client_identity("GxWindowClass", "renamed.exe", ""));
        assert!(is_client_identity("custom", "WOW-64.EXE", ""));
        assert!(is_client_identity("custom", "private-client.exe", "客户端"));
    }
}
