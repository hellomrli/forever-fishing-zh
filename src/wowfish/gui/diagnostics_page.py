"""Game setup, audio tests, icon calibration, region picker and diagnostics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wowfish.gui import image
from wowfish.gui.presenter import (
    CalibrateDone,
    DiagnosticsDone,
    Presenter,
    PreviewDone,
    VerifyDone,
    View,
    WorkerFailed,
)
from wowfish.gui.widgets import ScrollFrame, copy_text, set_entry, wrapped_label
from wowfish.messages import error_text

CHECKLIST = (
    "1. 站在水边，将“钓鱼”放到动作条并绑定抛竿键。\n"
    "2. 在游戏按键设置中，将“与目标互动”绑定到同一个交互键。\n"
    "3. 开启自动拾取；执行下方命令，显示浮漂上的金色齿轮。\n"
    "4. 保持游戏窗口可见，让浮漂处于互动距离内；将音乐和环境音调低，保留音效。\n"
    "5. 首次开始会自动查找图标大小；识别不稳定时，再进行图标校准、框选和录音验证。"
)
ICON_COMMAND = "/console SoftTargetIconGameObject 1"
LURE_MACRO = "/use 闪光的小珠\n/use 16"


class RegionPicker:
    def __init__(self, parent: Any, png: bytes, presenter: Presenter, done: Any) -> None:
        import tkinter as tk

        import ttkbootstrap as tb

        self.p, self.done = presenter, done
        self.window = tb.Toplevel(master=parent, title="框选浮漂识别范围", resizable=(False, False))
        self.window.transient(parent.winfo_toplevel())
        f = tb.Frame(self.window, padding=12)
        f.pack(fill="both", expand=True)
        wrapped_label(
            f,
            "在截图中拖动鼠标，框选自己的浮漂可能落入的水面。范围随游戏窗口缩放。",
            bootstyle="info",
        ).pack(fill="x", pady=(0, 8))
        self.photo = image.photo_from_png(png, max_width=740, max_height=360)
        self.width, self.height = self.photo.width(), self.photo.height()
        self.canvas = tk.Canvas(
            f, width=self.width, height=self.height, highlightthickness=0, cursor="crosshair"
        )
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        v = self.p.cfg.vision
        self.box = (
            v.search_left * self.width,
            v.search_top * self.height,
            v.search_right * self.width,
            v.search_bottom * self.height,
        )
        self.rect = self.canvas.create_rectangle(*self.box, outline="#4ade80", width=2)
        self.anchor = (0.0, 0.0)
        self.canvas.bind("<ButtonPress-1>", self._begin)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._drag)
        row = tb.Frame(f)
        row.pack(fill="x", pady=(10, 0))
        tb.Button(row, text="保存识别范围", command=self._save, bootstyle="success").pack(
            side="left"
        )
        tb.Button(row, text="取消", command=self.window.destroy, bootstyle="secondary").pack(
            side="left", padx=8
        )
        self.note = wrapped_label(f)
        self.note.pack(fill="x", pady=(6, 0))

    def _point(self, event: Any) -> tuple[float, float]:
        return max(0, min(self.width, event.x)), max(0, min(self.height, event.y))

    def _begin(self, event: Any) -> None:
        self.anchor = self._point(event)
        self._drag(event)

    def _drag(self, event: Any) -> None:
        x, y = self._point(event)
        ax, ay = self.anchor
        self.box = (min(x, ax), min(y, ay), max(x, ax), max(y, ay))
        self.canvas.coords(self.rect, *self.box)

    def _save(self) -> None:
        x0, y0, x1, y1 = self.box
        if x1 - x0 < 10 or y1 - y0 < 10:
            self.note.configure(
                text="范围太小，请重新框选至少 10 × 10 像素的水面。", bootstyle="warning"
            )
            return
        try:
            self.p.save_search_region(
                x0 / self.width, y0 / self.height, x1 / self.width, y1 / self.height
            )
            self.done()
            self.window.destroy()
        except Exception as e:
            self.note.configure(text=error_text(str(e)), bootstyle="danger")


class DiagnosticsPage:
    def __init__(self, parent: Any, presenter: Presenter) -> None:
        import tkinter as tk

        import ttkbootstrap as tb

        self.p = presenter
        self._audio_status_shown = False
        self._cal_status_shown = False
        self.scroll = ScrollFrame(parent)
        self.frame, f = self.scroll.frame, self.scroll.content
        self.picker: RegionPicker | None = None
        game = tb.Labelframe(f, text="游戏内准备", padding=12)
        game.pack(fill="x", pady=(0, 6))
        wrapped_label(game, CHECKLIST).pack(fill="x")
        command = tb.Frame(game)
        command.pack(fill="x", pady=(10, 4))
        tb.Label(command, text=ICON_COMMAND).pack(side="left")
        tb.Button(
            command,
            text="复制图标命令",
            command=lambda: self._copy(ICON_COMMAND),
            bootstyle="secondary",
        ).pack(side="left", padx=8)
        macros = tb.Frame(game)
        macros.pack(fill="x", pady=4)
        tb.Button(
            macros,
            text="复制钓鱼宏",
            command=lambda: self._copy("/cast 钓鱼"),
            bootstyle="secondary",
        ).pack(side="left")
        tb.Button(
            macros, text="复制鱼饵宏", command=lambda: self._copy(LURE_MACRO), bootstyle="secondary"
        ).pack(side="left", padx=8)
        tb.Button(
            macros, text="复制退出宏", command=lambda: self._copy("/quit"), bootstyle="secondary"
        ).pack(side="left")
        self.copy_note = wrapped_label(
            game, "鱼饵宏中的物品名称需改成背包里实际使用的鱼饵。", bootstyle="secondary"
        )
        self.copy_note.pack(fill="x", pady=(4, 0))

        audio = tb.Labelframe(f, text="音频来源与水花验证", padding=12)
        audio.pack(fill="x", pady=6)
        audio.columnconfigure(0, weight=1)
        self.device = tb.Combobox(audio, state="readonly", width=48)
        self.device.grid(row=0, column=0, sticky="ew")
        self.refresh = tb.Button(
            audio, text="刷新设备", command=self.refresh_devices, bootstyle="secondary"
        )
        self.refresh.grid(row=0, column=1, padx=(8, 0))
        self.devices: list[Any] = []
        self.refresh_devices()
        self.fallback = tk.BooleanVar(value=presenter.cfg.audio.fallback_to_device)
        tb.Checkbutton(
            audio,
            text="游戏进程音频不可用时，自动使用默认系统回环",
            variable=self.fallback,
            bootstyle="info-round-toggle",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=8)
        actions = tb.Frame(audio)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.listen = tb.Button(actions, text="保存并试听", command=self._listen, bootstyle="info")
        self.listen.pack(side="left")
        tb.Button(actions, text="停止试听", command=self.p.stop_meter, bootstyle="secondary").pack(
            side="left", padx=8
        )
        self.verify = tb.Button(
            actions, text="录音验证（60 秒）", command=self._verify, bootstyle="info"
        )
        self.verify.pack(side="left")
        self.cancel = tb.Button(
            actions, text="取消任务", command=self._cancel, bootstyle="warning", state="disabled"
        )
        self.cancel.pack(side="left", padx=8)
        self.meter = tb.Progressbar(audio, maximum=100, bootstyle="info")
        self.meter.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        self.level = wrapped_label(audio, "点击试听后显示音量和水花得分。", bootstyle="secondary")
        self.level.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.source = wrapped_label(
            audio, "启动时会实际检查捕获能力；系统回环包含其他程序的声音。", bootstyle="secondary"
        )
        self.source.grid(row=5, column=0, columnspan=2, sticky="ew", pady=4)
        threshold = tb.Frame(audio)
        threshold.grid(row=6, column=0, columnspan=2, sticky="ew", pady=8)
        tb.Label(threshold, text="水花得分阈值").pack(side="left")
        self.bite = tb.Entry(threshold, width=8)
        set_entry(self.bite, f"{presenter.cfg.audio.bite_threshold:.2f}")
        self.bite.pack(side="left", padx=8)
        tb.Button(threshold, text="保存阈值", command=self._save_bite, bootstyle="success").pack(
            side="left"
        )
        self.audio_note = wrapped_label(
            audio, "录音时请切回游戏，手动钓到一次咬钩；完成后给出阈值建议。"
        )
        self.audio_note.grid(row=7, column=0, columnspan=2, sticky="ew")

        cal = tb.Labelframe(f, text="图标校准与识别范围", padding=12)
        cal.pack(fill="x", pady=6)
        row = tb.Frame(cal)
        row.pack(fill="x")
        self.calibrate = tb.Button(
            row, text="校准金色齿轮", command=self.p.grab_and_calibrate, bootstyle="info"
        )
        self.calibrate.pack(side="left")
        self.save_cal = tb.Button(
            row, text="保存校准", command=self._save_cal, bootstyle="success", state="disabled"
        )
        self.save_cal.pack(side="left", padx=8)
        self.preview_button = tb.Button(
            row, text="截图并框选水面", command=self.p.grab_preview, bootstyle="info"
        )
        self.preview_button.pack(side="left")
        tb.Button(row, text="恢复全窗口", command=self._reset_region, bootstyle="secondary").pack(
            side="left", padx=8
        )
        self.cal_note = wrapped_label(
            cal, "校准最多观察 30 秒；点击后切回游戏并抛竿，让金色齿轮出现。"
        )
        self.cal_note.pack(fill="x", pady=(10, 4))
        self.region_note = wrapped_label(cal, bootstyle="secondary")
        self.region_note.pack(fill="x")
        self.cal_image = tb.Label(cal)
        self.cal_image.pack(anchor="w", pady=(8, 0))
        self._photo = None
        self._update_region_note()

        diagnostics = tb.Labelframe(f, text="环境诊断与日志", padding=12)
        diagnostics.pack(fill="x", pady=6)
        row = tb.Frame(diagnostics)
        row.pack(fill="x")
        self.doctor = tb.Button(
            row, text="运行环境诊断", command=self.p.run_diagnostics, bootstyle="info"
        )
        self.doctor.pack(side="left")
        self.export = tb.Button(
            row, text="导出运行日志", command=self._export, bootstyle="secondary"
        )
        self.export.pack(side="left", padx=8)
        self.diagnostic_note = wrapped_label(
            diagnostics, "诊断会检查窗口、音频、截图组件、按键和配置。"
        )
        self.diagnostic_note.pack(fill="x", pady=(8, 0))
        self.error = wrapped_label(f, bootstyle="warning")
        self.error.pack(fill="x", pady=8)

    def _copy(self, text: str) -> None:
        copy_text(self.frame, text)
        self.copy_note.configure(
            text="已复制到剪贴板，可在游戏聊天框或宏编辑器中粘贴。", bootstyle="success"
        )

    def refresh_devices(self) -> None:
        previous = self._selected_device() if self.devices else None
        self.devices = self.p.audio_devices()
        self.device.configure(values=[d.name for d in self.devices] or ["未找到回环设备"])
        wanted = previous or self.p.cfg.audio.selector
        pick = next((i for i, d in enumerate(self.devices) if wanted in (d.id, d.name)), 0)
        self.device.current(pick)

    def _selected_device(self) -> str | None:
        index = self.device.current()
        return self.devices[index].id if 0 <= index < len(self.devices) else None

    def _save_device(self) -> str:
        selector = self._selected_device()
        if selector is None:
            raise ValueError("没有可用音频设备，请连接声音输出设备后刷新")
        self.p.save_audio_settings(
            capture="game" if selector == "game" else "device",
            device=self.p.cfg.audio.device if selector == "game" else selector,
            fallback_to_device=self.fallback.get(),
        )
        return selector

    def _listen(self) -> None:
        try:
            selector = self._save_device()
            self.audio_note.configure(
                text="设置已保存，正在打开音频来源；请切回游戏观察音量。", bootstyle="info"
            )
            self.p.start_meter(selector)
        except Exception as e:
            self.audio_note.configure(text=error_text(str(e)), bootstyle="danger")

    def _verify(self) -> None:
        try:
            selector = self._save_device()
            self.audio_note.configure(text="正在开始录音，请切回游戏手动钓鱼。", bootstyle="info")
            self.p.record_and_verify(60.0, selector)
        except Exception as e:
            self.audio_note.configure(text=error_text(str(e)), bootstyle="danger")

    def _cancel(self) -> None:
        self.p.cancel_workers()
        self._audio_status_shown = self._cal_status_shown = True
        self.audio_note.configure(text="已请求取消，设备正在关闭。")
        self.cal_note.configure(text="已请求取消，当前任务结束后可重试。")

    def _save_bite(self) -> None:
        try:
            threshold = self.p.save_bite_threshold(self.bite.get())
            self.audio_note.configure(
                text=f"已保存阈值 {threshold:.2f}；下一次开始或试听时生效。", bootstyle="success"
            )
        except Exception as e:
            self.audio_note.configure(text=error_text(str(e)), bootstyle="danger")

    def _save_cal(self) -> None:
        try:
            self.p.save_calibration()
            self.cal_note.configure(
                text=f"已保存图标校准：{self.p.cfg.vision.icon_size_px} 像素。", bootstyle="success"
            )
        except Exception as e:
            self.cal_note.configure(text=error_text(str(e)), bootstyle="danger")

    def _update_region_note(self) -> None:
        v = self.p.cfg.vision
        size = f"{v.icon_size_px} 像素" if v.icon_size_px else "开始时自动测量"
        self.region_note.configure(
            text=f"已保存图标：{size}；识别范围：左 {v.search_left:.0%}、上 {v.search_top:.0%}、"
            f"右 {v.search_right:.0%}、下 {v.search_bottom:.0%}"
        )

    def _reset_region(self) -> None:
        try:
            self.p.save_search_region(0, 0, 1, 1)
            self._update_region_note()
            self.cal_note.configure(text="识别范围已恢复为整个游戏窗口。", bootstyle="success")
        except Exception as e:
            self.cal_note.configure(text=error_text(str(e)), bootstyle="danger")

    def _export(self) -> None:
        from tkinter import filedialog

        from wowfish import paths

        target = filedialog.asksaveasfilename(
            parent=self.frame,
            title="导出运行日志",
            initialfile="ForeverFishing-运行日志.log",
            defaultextension=".log",
            filetypes=[("日志文件", "*.log"), ("所有文件", "*.*")],
        )
        if not target:
            return
        try:
            Path(target).write_bytes(paths.log_file().read_bytes())
            self.diagnostic_note.configure(text=f"运行日志已导出到：{target}", bootstyle="success")
        except Exception as e:
            self.diagnostic_note.configure(text=error_text(str(e)), bootstyle="danger")

    def render(self, view: View) -> None:
        for button in (
            self.calibrate,
            self.preview_button,
            self.verify,
            self.doctor,
            self.listen,
            self.refresh,
        ):
            button.configure(state="disabled" if view.running or view.working else "normal")
        self.cancel.configure(state="normal" if view.working else "disabled")
        self.meter.configure(value=max(0.0, min(100.0, (view.level_dbfs + 60) / 60 * 100)))
        best = max(view.last_scores.values(), default=-1.0)
        self.level.configure(
            text=("静音" if view.level_dbfs <= -60 else f"音量 {view.level_dbfs:.1f} dBFS")
            + (f"，最高水花得分 {max(0, best):.2f}" if view.listening else "；当前未试听")
        )
        if view.audio_name:
            self.source.configure(text=f"实际音频来源：{view.audio_name}")
        if view.verify_status:
            self.audio_note.configure(text=view.verify_status, bootstyle="info")
            self._audio_status_shown = True
        elif view.activity_status:
            self.audio_note.configure(text=view.activity_status, bootstyle="info")
            self._audio_status_shown = True
        elif self._audio_status_shown:
            self._audio_status_shown = False
            self.audio_note.configure(
                text=(
                    "正在试听，请切回游戏观察音量和水花得分。"
                    if view.listening
                    else "任务已结束，可以重新试听或录音验证。"
                ),
                bootstyle="secondary",
            )
        if view.calibrate_status:
            self.cal_note.configure(text=view.calibrate_status, bootstyle="info")
            self._cal_status_shown = True
        elif self._cal_status_shown and not view.working:
            self._cal_status_shown = False
            self.cal_note.configure(text="任务已结束，可以重新校准或截图。", bootstyle="secondary")
        self.error.configure(text=view.error[:320])
        self._update_region_note()
        for item, line in zip(view.log_events, view.new_lines, strict=True):
            match item:
                case CalibrateDone(calibration=cal):
                    self._photo = image.photo_from_png(item.thumbnail_png, max_height=200)
                    self.cal_image.configure(image=self._photo)
                    self.cal_note.configure(
                        text=line, bootstyle="success" if cal.valid else "warning"
                    )
                    self.save_cal.configure(state="normal" if cal.valid else "disabled")
                case VerifyDone(report=report):
                    self.audio_note.configure(
                        text=line, bootstyle="success" if report.stands_out else "warning"
                    )
                    if report.stands_out:
                        set_entry(self.bite, f"{report.threshold:.2f}")
                case PreviewDone(thumbnail_png=png):
                    if self.picker is not None and self.picker.window.winfo_exists():
                        self.picker.window.destroy()
                    self.picker = RegionPicker(self.frame, png, self.p, self._update_region_note)
                case DiagnosticsDone(results=results):
                    from wowfish.doctor import render

                    self.diagnostic_note.configure(text=render(results))
                case WorkerFailed(what=what):
                    note = self.audio_note if what in ("试听", "录音验证") else self.cal_note
                    note.configure(text=line, bootstyle="danger")
