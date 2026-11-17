use super::*;
use ::windows::{
    core::{w, BOOL, PCWSTR},
    Win32::{
        Foundation::{HWND, LPARAM, POINT, RECT, WPARAM},
        Graphics::Gdi::*,
        System::Threading::GetCurrentThreadId,
        UI::{
            HiDpi::{SetThreadDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2},
            Input::KeyboardAndMouse::*,
            WindowsAndMessaging::*,
        },
    },
};
use anyhow::{ensure, Context};
use std::{
    sync::mpsc::{self, Receiver},
    thread::{self, JoinHandle},
    time::Duration,
};

fn hwnd(window: &Window) -> HWND {
    HWND(window.handle as *mut std::ffi::c_void)
}
fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}

fn window_ref(handle: HWND) -> Result<Option<Window>> {
    unsafe {
        SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        if !IsWindow(Some(handle)).as_bool() {
            return Ok(None);
        }
        let mut pid = 0;
        GetWindowThreadProcessId(handle, Some(&mut pid));
        let length = GetWindowTextLengthW(handle).max(0) as usize;
        let mut text = vec![0u16; length + 1];
        let count = GetWindowTextW(handle, &mut text).max(0) as usize;
        let title = String::from_utf16_lossy(&text[..count]);
        let mut rect = RECT::default();
        GetClientRect(handle, &mut rect)?;
        let mut point = POINT::default();
        ensure!(
            ClientToScreen(handle, &mut point).as_bool(),
            "无法读取窗口位置"
        );
        Ok(Some(Window {
            handle: handle.0 as usize,
            pid,
            title,
            x: point.x,
            y: point.y,
            width: (rect.right - rect.left).max(0) as u32,
            height: (rect.bottom - rect.top).max(0) as u32,
            minimized: IsIconic(handle).as_bool(),
        }))
    }
}

pub fn windows(title: &str) -> Result<Vec<Window>> {
    unsafe extern "system" fn enumerate(handle: HWND, data: LPARAM) -> BOOL {
        let found = &mut *(data.0 as *mut Vec<Window>);
        if IsWindowVisible(handle).as_bool() {
            if let Ok(Some(window)) = window_ref(handle) {
                found.push(window);
            }
        }
        BOOL(1)
    }
    let mut found: Vec<Window> = Vec::new();
    unsafe {
        EnumWindows(
            Some(enumerate),
            LPARAM(&mut found as *mut Vec<Window> as isize),
        )?;
    }
    found.retain(|w| {
        is_game_title(&w.title, title)
            && !(w.pid == std::process::id() && w.title.starts_with("Forever 钓鱼助手"))
    });
    found.sort_by(|a, b| a.pid.cmp(&b.pid).then(a.handle.cmp(&b.handle)));
    Ok(found)
}

pub fn refresh(window: &Window) -> Result<Option<Window>> {
    Ok(window_ref(hwnd(window))?.filter(|w| w.pid == window.pid))
}

pub fn focused(window: &Window) -> bool {
    unsafe { !IsIconic(hwnd(window)).as_bool() && GetForegroundWindow() == hwnd(window) }
}

pub fn capture(window: &Window, region: Region) -> Result<Frame> {
    let window = refresh(window)?.context("游戏窗口已关闭")?;
    ensure!(!window.minimized, "游戏窗口已最小化，请恢复后重试");
    let (x0, y0, x1, y1) = region.pixels(window.width, window.height);
    let (w, h) = (x1.saturating_sub(x0), y1.saturating_sub(y0));
    ensure!(
        w > 0 && h > 0 && w as u64 * h as u64 <= 64_000_000,
        "截图范围无效"
    );
    unsafe {
        struct Gdi {
            screen: HDC,
            memory: HDC,
            bitmap: HBITMAP,
            old: HGDIOBJ,
        }
        impl Drop for Gdi {
            fn drop(&mut self) {
                unsafe {
                    if !self.old.0.is_null() {
                        SelectObject(self.memory, self.old);
                    }
                    if !self.bitmap.0.is_null() {
                        let _ = DeleteObject(HGDIOBJ(self.bitmap.0));
                    }
                    if !self.memory.0.is_null() {
                        let _ = DeleteDC(self.memory);
                    }
                    if !self.screen.0.is_null() {
                        ReleaseDC(None, self.screen);
                    }
                }
            }
        }
        let mut gdi = Gdi {
            screen: GetDC(None),
            memory: HDC::default(),
            bitmap: HBITMAP::default(),
            old: HGDIOBJ::default(),
        };
        ensure!(!gdi.screen.0.is_null(), "无法获取桌面画面");
        gdi.memory = CreateCompatibleDC(Some(gdi.screen));
        ensure!(!gdi.memory.0.is_null(), "无法创建截图设备");
        gdi.bitmap = CreateCompatibleBitmap(gdi.screen, w as i32, h as i32);
        ensure!(!gdi.bitmap.0.is_null(), "无法创建截图位图");
        gdi.old = SelectObject(gdi.memory, HGDIOBJ(gdi.bitmap.0));
        BitBlt(
            gdi.memory,
            0,
            0,
            w as i32,
            h as i32,
            Some(gdi.screen),
            window.x + x0 as i32,
            window.y + y0 as i32,
            SRCCOPY | CAPTUREBLT,
        )?;
        SelectObject(gdi.memory, gdi.old);
        gdi.old = HGDIOBJ::default();
        let mut info = BITMAPINFO::default();
        info.bmiHeader.biSize = std::mem::size_of::<BITMAPINFOHEADER>() as u32;
        info.bmiHeader.biWidth = w as i32;
        info.bmiHeader.biHeight = -(h as i32);
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        info.bmiHeader.biCompression = BI_RGB.0;
        let mut pixels = vec![0u8; (w * h * 4) as usize];
        ensure!(
            GetDIBits(
                gdi.screen,
                gdi.bitmap,
                0,
                h,
                Some(pixels.as_mut_ptr().cast()),
                &mut info,
                DIB_RGB_COLORS
            ) == h as i32,
            "截图读取失败"
        );
        let rgb: Vec<_> = pixels
            .as_chunks::<4>()
            .0
            .iter()
            .flat_map(|p| [p[2], p[1], p[0]])
            .collect();
        Frame::from_raw(w, h, rgb).context("截图尺寸不匹配")
    }
}

pub fn press(window: &Window, key: &str, hold_ms: u32, cancel: &Cancel) -> Result<bool> {
    if cancel.is_cancelled() || !focused(window) {
        return Ok(false);
    }
    let code = config::key_code(key).context("不支持的按键")?;
    unsafe {
        let down = INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: VIRTUAL_KEY(code),
                    ..Default::default()
                },
            },
        };
        ensure!(
            SendInput(&[down], std::mem::size_of::<INPUT>() as i32) == 1,
            "发送按键失败，请确认助手与游戏使用相同的权限级别"
        );
        // Key-up is mandatory even if cancellation or focus loss happens during the hold.
        cancel.wait(Duration::from_millis(hold_ms as u64));
        let up = INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: VIRTUAL_KEY(code),
                    dwFlags: KEYEVENTF_KEYUP,
                    ..Default::default()
                },
            },
        };
        ensure!(
            SendInput(&[up], std::mem::size_of::<INPUT>() as i32) == 1,
            "释放按键失败"
        );
    }
    Ok(true)
}

pub fn show_error(text: &str) {
    let text = wide(text);
    unsafe {
        MessageBoxW(
            None,
            PCWSTR(text.as_ptr()),
            w!("Forever 钓鱼助手 Rust"),
            MB_OK | MB_ICONERROR,
        );
    }
}

pub struct Hotkeys {
    receiver: Receiver<Hotkey>,
    cancel: Cancel,
    thread: Option<JoinHandle<()>>,
    thread_id: u32,
}

impl Hotkeys {
    pub fn new(game: &config::Game) -> Result<Self> {
        let keys = [game.pause_hotkey.clone(), game.stop_hotkey.clone()];
        let (send, receiver) = mpsc::channel();
        let (ready, opened) = mpsc::sync_channel(1);
        let cancel = Cancel::default();
        let stop = cancel.clone();
        let thread = thread::spawn(move || unsafe {
            let mut registered = Vec::new();
            for (i, key) in keys.iter().enumerate() {
                if key.trim().is_empty() {
                    continue;
                }
                let result = RegisterHotKey(
                    None,
                    i as i32 + 1,
                    MOD_NOREPEAT,
                    config::key_code(key).unwrap_or(0) as u32,
                );
                if let Err(error) = result {
                    for id in registered {
                        let _ = UnregisterHotKey(None, id);
                    }
                    let _ = ready.send(Err(format!(
                        "快捷键 {key} 注册失败，可能被其他程序占用：{error}"
                    )));
                    return;
                }
                registered.push(i as i32 + 1);
            }
            let mut message = MSG::default();
            let _ = PeekMessageW(&mut message, None, 0, 0, PM_NOREMOVE);
            let _ = ready.send(Ok(GetCurrentThreadId()));
            while !stop.is_cancelled() {
                while PeekMessageW(&mut message, None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE).as_bool() {
                    if message.wParam.0 == 1 {
                        let _ = send.send(Hotkey::Pause);
                    } else if message.wParam.0 == 2 {
                        let _ = send.send(Hotkey::Stop);
                    }
                }
                stop.wait(Duration::from_millis(20));
            }
            for id in registered {
                let _ = UnregisterHotKey(None, id);
            }
        });
        let startup = opened
            .recv_timeout(Duration::from_secs(3))
            .context("快捷键线程启动超时")
            .and_then(|result| result.map_err(|s| anyhow::anyhow!(s)));
        let thread_id = match startup {
            Ok(id) => id,
            Err(error) => {
                cancel.cancel();
                let _ = thread.join();
                return Err(error);
            }
        };
        Ok(Self {
            receiver,
            cancel,
            thread: Some(thread),
            thread_id,
        })
    }
    pub fn poll(&self) -> Vec<Hotkey> {
        self.receiver.try_iter().collect()
    }
}

impl Drop for Hotkeys {
    fn drop(&mut self) {
        self.cancel.cancel();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

pub fn native_smoke() -> Result<serde_json::Value> {
    unsafe {
        let handle = CreateWindowExW(
            WINDOW_EX_STYLE::default(),
            w!("STATIC"),
            w!("魔兽世界 Forever Rust 自检"),
            WS_OVERLAPPEDWINDOW | WS_VISIBLE,
            60,
            80,
            480,
            360,
            None,
            None,
            None,
            None,
        )?;
        struct TestWindow(HWND);
        impl Drop for TestWindow {
            fn drop(&mut self) {
                unsafe {
                    let _ = DestroyWindow(self.0);
                }
            }
        }
        let _window = TestWindow(handle);
        let original = window_ref(handle)?.context("无法创建自检窗口")?;
        ensure!(
            windows("Rust 自检")?
                .iter()
                .any(|w| w.handle == original.handle && w.pid == original.pid),
            "中文窗口枚举失败"
        );
        let first = capture(&original, Region::default())?;
        ensure!(
            first.dimensions() == (original.width, original.height),
            "原生截图尺寸错误"
        );
        SetWindowPos(handle, None, 130, 140, 640, 420, SWP_NOZORDER)?;
        let resized = refresh(&original)?.context("移动后窗口丢失")?;
        let second = capture(&original, Region::default())?;
        ensure!(
            resized.x != original.x && resized.width != original.width,
            "移动或缩放未同步"
        );
        ensure!(
            second.dimensions() == (resized.width, resized.height),
            "缩放后截图未同步"
        );
        let _ = ShowWindow(handle, SW_MINIMIZE);
        ensure!(
            refresh(&original)?.context("最小化后身份丢失")?.minimized,
            "最小化状态错误"
        );
        let _ = ShowWindow(handle, SW_RESTORE);
        ensure!(
            !refresh(&original)?.context("恢复后身份丢失")?.minimized,
            "窗口恢复失败"
        );
        // Verify the shipped F8/F9 defaults without sending real keystrokes.
        let hotkeys = Hotkeys::new(&config::Game::default())?;
        PostThreadMessageW(hotkeys.thread_id, WM_HOTKEY, WPARAM(1), LPARAM(0))?;
        PostThreadMessageW(hotkeys.thread_id, WM_HOTKEY, WPARAM(2), LPARAM(0))?;
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        let mut delivered = Vec::new();
        while delivered.len() < 2 && std::time::Instant::now() < deadline {
            delivered.extend(hotkeys.poll());
            thread::sleep(Duration::from_millis(10));
        }
        ensure!(
            delivered == vec![Hotkey::Pause, Hotkey::Stop],
            "原生快捷键消息未送达"
        );
        Ok(
            serde_json::json!({ "supported": true, "chinese_window": true, "capture": true,
            "move_resize": true, "minimize_restore": true, "hotkeys": true }),
        )
    }
}
