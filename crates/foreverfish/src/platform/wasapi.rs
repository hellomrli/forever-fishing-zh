use super::*;
use crate::audio::{Resampler, RATE};
use ::windows::{
    core::{implement, w, Interface, Ref, HRESULT, PCWSTR, PWSTR},
    Win32::{
        Devices::FunctionDiscovery::PKEY_Device_FriendlyName,
        Foundation::{CloseHandle, HANDLE},
        Media::Audio::*,
        System::{
            Com::{StructuredStorage::*, *},
            Threading::CreateEventW,
            Variant::VT_BLOB,
        },
    },
};
use anyhow::{bail, ensure, Context};
use std::{
    mem::ManuallyDrop,
    sync::{mpsc, Arc},
    time::{Duration, Instant},
};

struct Com;
impl Com {
    fn new() -> Result<Self> {
        unsafe {
            CoInitializeEx(None, COINIT_MULTITHREADED).ok()?;
        }
        Ok(Self)
    }
}
impl Drop for Com {
    fn drop(&mut self) {
        unsafe {
            CoUninitialize();
        }
    }
}

fn text(pointer: PWSTR) -> Result<String> {
    let result = unsafe { pointer.to_string() };
    unsafe {
        CoTaskMemFree(Some(pointer.0.cast()));
    }
    Ok(result?)
}

fn friendly(device: &IMMDevice) -> Result<String> {
    unsafe {
        let store = device.OpenPropertyStore(STGM_READ)?;
        let mut value = store.GetValue(&PKEY_Device_FriendlyName)?;
        let result = PropVariantToStringAlloc(&value)
            .map_err(anyhow::Error::from)
            .and_then(text);
        let _ = PropVariantClear(&mut value);
        result
    }
}

fn enumerator() -> Result<IMMDeviceEnumerator> {
    Ok(unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)? })
}

fn list(enumerator: &IMMDeviceEnumerator) -> Result<Vec<Device>> {
    unsafe {
        let default = enumerator
            .GetDefaultAudioEndpoint(eRender, eConsole)
            .ok()
            .and_then(|d| d.GetId().ok())
            .and_then(|p| text(p).ok());
        let collection = enumerator.EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE)?;
        let mut devices = Vec::new();
        for i in 0..collection.GetCount()? {
            let device = collection.Item(i)?;
            let id = text(device.GetId()?)?;
            let name = friendly(&device).unwrap_or_else(|_| id.clone());
            let is_default = default.as_ref() == Some(&id);
            devices.push(Device {
                id,
                name,
                default: is_default,
            });
        }
        Ok(devices)
    }
}

pub fn devices() -> Result<Vec<Device>> {
    let _com = Com::new()?;
    list(&enumerator()?)
}

struct ActivationStorage {
    _params: Box<AUDIOCLIENT_ACTIVATION_PARAMS>,
    variant: PROPVARIANT,
}
// The blob points into an immutable boxed parameter block owned by this allocation.
// Windows keeps a reference to the agile completion handler until activation completes.
unsafe impl Send for ActivationStorage {}
unsafe impl Sync for ActivationStorage {}

#[implement(IActivateAudioInterfaceCompletionHandler)]
struct Completion {
    sender: mpsc::SyncSender<()>,
    _storage: Arc<ActivationStorage>,
}
impl IActivateAudioInterfaceCompletionHandler_Impl for Completion_Impl {
    fn ActivateCompleted(
        &self,
        _operation: Ref<'_, IActivateAudioInterfaceAsyncOperation>,
    ) -> ::windows::core::Result<()> {
        let _ = self.sender.try_send(());
        Ok(())
    }
}

fn process_client(pid: u32, cancel: &Cancel) -> Result<IAudioClient> {
    let params = Box::new(AUDIOCLIENT_ACTIVATION_PARAMS {
        ActivationType: AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        Anonymous: AUDIOCLIENT_ACTIVATION_PARAMS_0 {
            ProcessLoopbackParams: AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS {
                TargetProcessId: pid,
                ProcessLoopbackMode: PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
            },
        },
    });
    let variant = PROPVARIANT {
        Anonymous: PROPVARIANT_0 {
            Anonymous: ManuallyDrop::new(PROPVARIANT_0_0 {
                vt: VT_BLOB,
                Anonymous: PROPVARIANT_0_0_0 {
                    blob: BLOB {
                        cbSize: std::mem::size_of::<AUDIOCLIENT_ACTIVATION_PARAMS>() as u32,
                        pBlobData: (&*params as *const AUDIOCLIENT_ACTIVATION_PARAMS)
                            .cast_mut()
                            .cast(),
                    },
                },
                ..Default::default()
            }),
        },
    };
    let storage = Arc::new(ActivationStorage {
        _params: params,
        variant,
    });
    let (sender, receiver) = mpsc::sync_channel(1);
    let handler: IActivateAudioInterfaceCompletionHandler = Completion {
        sender,
        _storage: storage.clone(),
    }
    .into();
    let operation = unsafe {
        ActivateAudioInterfaceAsync(
            w!("VAD\\Process_Loopback"),
            &IAudioClient::IID,
            Some(&storage.variant),
            &handler,
        )
    }
    .context("进程音频捕获需要 Windows Build 20348+，请尝试系统回环")?;
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        cancel.check()?;
        match receiver.recv_timeout(Duration::from_millis(50)) {
            Ok(()) => break,
            Err(mpsc::RecvTimeoutError::Timeout) if Instant::now() < deadline => continue,
            _ => bail!("游戏进程音频打开超过 5 秒，请尝试系统回环"),
        }
    }
    cancel.check()?;
    let mut result = HRESULT(0);
    let mut unknown = None;
    unsafe {
        operation.GetActivateResult(&mut result, &mut unknown)?;
    }
    result.ok().context("游戏进程音频激活失败")?;
    Ok(unknown.context("进程音频没有返回客户端")?.cast()?)
}

#[derive(Clone, Copy)]
struct Format {
    channels: usize,
    rate: u32,
    bits: usize,
    stride: usize,
    float: bool,
}

pub struct Loopback {
    capture: Option<IAudioCaptureClient>,
    client: Option<IAudioClient>,
    event: Option<HANDLE>,
    resampler: Resampler,
    format: Format,
    name: String,
    fallback: String,
    _com: Com,
}

impl Loopback {
    fn process(pid: u32, cancel: &Cancel) -> Result<Self> {
        let com = Com::new()?;
        let client = process_client(pid, cancel)?;
        let format = WAVEFORMATEX {
            wFormatTag: 1,
            nChannels: 2,
            nSamplesPerSec: 44100,
            nAvgBytesPerSec: 176400,
            nBlockAlign: 4,
            wBitsPerSample: 16,
            cbSize: 0,
        };
        Self::initialize(
            com,
            client,
            &format,
            Format {
                channels: 2,
                rate: 44100,
                bits: 16,
                stride: 4,
                float: false,
            },
            format!("游戏进程音频（PID {pid}）"),
            true,
            cancel,
        )
    }

    fn device(selector: &str, cancel: &Cancel) -> Result<Self> {
        let com = Com::new()?;
        let enumerator = enumerator()?;
        cancel.check()?;
        let device = unsafe {
            if selector.trim().is_empty() {
                enumerator.GetDefaultAudioEndpoint(eRender, eConsole)?
            } else {
                let devices = list(&enumerator)?;
                let found: Vec<_> = devices
                    .iter()
                    .filter(|d| d.id == selector || d.name == selector || d.name.contains(selector))
                    .collect();
                ensure!(
                    found.len() == 1,
                    "找不到唯一的音频设备，请刷新列表并选择完整名称"
                );
                let id: Vec<_> = found[0].id.encode_utf16().chain(Some(0)).collect();
                enumerator.GetDevice(PCWSTR(id.as_ptr()))?
            }
        };
        let name = format!(
            "系统回环 · {}",
            friendly(&device).unwrap_or_else(|_| "默认输出设备".into())
        );
        let client: IAudioClient = unsafe { device.Activate(CLSCTX_ALL, None)? };
        let pointer = unsafe { client.GetMixFormat()? };
        ensure!(!pointer.is_null(), "音频设备没有提供格式");
        struct MixFormat(*mut WAVEFORMATEX);
        impl Drop for MixFormat {
            fn drop(&mut self) {
                unsafe {
                    CoTaskMemFree(Some(self.0.cast()));
                }
            }
        }
        let _format = MixFormat(pointer);
        let raw = unsafe { *pointer };
        let float = if raw.wFormatTag == 0xfffe && raw.cbSize >= 22 {
            // WAVEFORMATEXTENSIBLE.SubFormat begins 24 bytes from the packed format.
            unsafe { std::ptr::read_unaligned(pointer.cast::<u8>().add(24).cast::<u32>()) == 3 }
        } else {
            raw.wFormatTag == 3
        };
        let format = Format {
            channels: raw.nChannels as usize,
            rate: raw.nSamplesPerSec,
            bits: raw.wBitsPerSample as usize,
            stride: raw.nBlockAlign as usize,
            float,
        };
        ensure!(
            format.channels > 0 && format.channels <= 32 && format.rate > 0,
            "不支持的音频声道或采样率"
        );
        ensure!(
            if float {
                matches!(format.bits, 32 | 64)
            } else {
                matches!(format.bits, 8 | 16 | 24 | 32)
            },
            "不支持的音频位深"
        );
        ensure!(
            format.stride >= format.channels * format.bits / 8,
            "音频帧长度无效"
        );
        Self::initialize(com, client, pointer, format, name, false, cancel)
    }

    fn initialize(
        com: Com,
        client: IAudioClient,
        raw: *const WAVEFORMATEX,
        format: Format,
        name: String,
        process: bool,
        cancel: &Cancel,
    ) -> Result<Self> {
        cancel.check()?;
        let mut source = Self {
            capture: None,
            client: Some(client),
            event: None,
            resampler: Resampler::new(format.rate, RATE),
            format,
            name,
            fallback: String::new(),
            _com: com,
        };
        let client = source.client.as_ref().unwrap();
        unsafe {
            let flags = AUDCLNT_STREAMFLAGS_LOOPBACK
                | if process {
                    AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                } else {
                    0
                };
            client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, 2_000_000, 0, raw, None)?;
            if process {
                let event = CreateEventW(None, false, false, None)?;
                source.event = Some(event);
                client.SetEventHandle(event)?;
            }
            source.capture = Some(client.GetService()?);
            cancel.check()?;
            client.Start()?;
        }
        Ok(source)
    }

    fn convert(&self, bytes: &[u8], frames: usize) -> Vec<f32> {
        let format = self.format;
        let size = format.bits / 8;
        let mut mono = Vec::with_capacity(frames);
        for frame in bytes.chunks_exact(format.stride).take(frames) {
            let mut sum = 0.0f32;
            for channel in 0..format.channels {
                let bytes = &frame[channel * size..channel * size + size];
                sum += if format.float {
                    if size == 4 {
                        f32::from_le_bytes(bytes.try_into().unwrap())
                    } else {
                        f64::from_le_bytes(bytes.try_into().unwrap()) as f32
                    }
                } else {
                    match size {
                        1 => (bytes[0] as f32 - 128.0) / 128.0,
                        2 => i16::from_le_bytes(bytes.try_into().unwrap()) as f32 / 32768.0,
                        3 => {
                            let value = (bytes[0] as i32)
                                | ((bytes[1] as i32) << 8)
                                | ((bytes[2] as i8 as i32) << 16);
                            value as f32 / 8388608.0
                        }
                        4 => i32::from_le_bytes(bytes.try_into().unwrap()) as f32 / 2147483648.0,
                        _ => 0.0,
                    }
                };
            }
            let value = sum / format.channels as f32;
            mono.push(if value.is_finite() {
                value.clamp(-1.0, 1.0)
            } else {
                0.0
            });
        }
        mono
    }
}

impl Capture for Loopback {
    fn name(&self) -> &str {
        &self.name
    }
    fn fallback_reason(&self) -> &str {
        &self.fallback
    }
    fn read(&mut self, cancel: &Cancel) -> Result<Vec<f32>> {
        cancel.check()?;
        let deadline = Instant::now() + Duration::from_millis(40);
        let mut mono = Vec::new();
        loop {
            cancel.check()?;
            let capture = self.capture.as_ref().unwrap();
            let next = unsafe { capture.GetNextPacketSize()? };
            if next == 0 {
                if !mono.is_empty() || Instant::now() >= deadline {
                    break;
                }
                cancel.wait(Duration::from_millis(5));
                continue;
            }
            let mut pointer = std::ptr::null_mut();
            let mut frames = 0;
            let mut flags = 0;
            unsafe {
                capture.GetBuffer(&mut pointer, &mut frames, &mut flags, None, None)?;
            }
            let values = if flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0 || pointer.is_null() {
                vec![0.0; frames as usize]
            } else {
                self.convert(
                    unsafe {
                        std::slice::from_raw_parts(pointer, frames as usize * self.format.stride)
                    },
                    frames as usize,
                )
            };
            unsafe {
                capture.ReleaseBuffer(frames)?;
            }
            mono.extend(values);
            if mono.len() >= self.format.rate as usize / 4 {
                break;
            }
        }
        Ok(self.resampler.feed(&mono))
    }
}

impl Drop for Loopback {
    fn drop(&mut self) {
        unsafe {
            if let Some(client) = &self.client {
                let _ = client.Stop();
            }
            self.capture.take();
            self.client.take();
            if let Some(event) = self.event.take() {
                let _ = CloseHandle(event);
            }
        }
    }
}

pub fn open_audio(
    audio: &config::Audio,
    pid: Option<u32>,
    cancel: &Cancel,
) -> Result<Box<dyn Capture>> {
    cancel.check()?;
    if audio.capture == "game" {
        let pid = pid.context("请先选择游戏客户端，再试听游戏进程音频")?;
        match Loopback::process(pid, cancel) {
            Ok(source) => return Ok(Box::new(source)),
            Err(error) => {
                cancel.check()?;
                if !audio.fallback_to_device {
                    return Err(error);
                }
                let mut source = Loopback::device(&audio.device, cancel)?;
                source.fallback = format!("游戏进程音频不可用，已回退系统回环：{error:#}");
                return Ok(Box::new(source));
            }
        }
    }
    Ok(Box::new(Loopback::device(&audio.device, cancel)?))
}
