"""Chinese settings with atomic saves and feedback beside each action."""

from __future__ import annotations

from typing import Any

from wowfish.gui.presenter import Presenter, View
from wowfish.gui.widgets import ScrollFrame, set_entry, wrapped_label
from wowfish.input.keys import normalize_key
from wowfish.messages import error_text


class SetupPage:
    def __init__(self, parent: Any, presenter: Presenter) -> None:
        import tkinter as tk

        import ttkbootstrap as tb

        self.p = presenter
        self.scroll = ScrollFrame(parent)
        self.frame, f = self.scroll.frame, self.scroll.content
        wrapped_label(f, "设置保存后，在下一次点击“开始钓鱼”时生效。", bootstyle="info").pack(
            fill="x", pady=(0, 10)
        )
        game = tb.Labelframe(f, text="游戏与按键", padding=12)
        game.pack(fill="x", pady=6)
        game.columnconfigure(1, weight=1)
        game.columnconfigure(3, weight=1)
        g = presenter.cfg.game
        self.cast = self._entry(game, "抛竿键", g.cast_key, 0, 0)
        self.interact = self._entry(game, "与目标互动键", g.interact_key, 0, 2)
        self.lure = self._entry(game, "鱼饵宏键（可留空）", g.lure_key, 1, 0)
        self.quit = self._entry(game, "退出宏键（可留空）", g.quit_key, 1, 2)
        self.minutes = self._entry(game, "钓鱼时限（分钟）", g.session_minutes, 2, 0)
        self.lure_minutes = self._entry(game, "补饵周期（分钟）", g.lure_minutes, 2, 2)
        self.pause_key = self._entry(game, "暂停 / 继续快捷键", g.pause_hotkey.upper(), 3, 0)
        self.stop_key = self._entry(game, "停止快捷键", g.stop_hotkey.upper(), 3, 2)
        tb.Label(game, text="窗口标题筛选").grid(row=4, column=0, sticky="w", pady=6)
        self.title = tb.Entry(game)
        set_entry(self.title, g.window_title)
        self.title.grid(row=4, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=6)
        hint = (
            "窗口标题留空时自动查找“魔兽世界”和“World of Warcraft”；多个客户端请在钓鱼页选择。\n"
            "时限 0 表示不限时。鱼饵和退出键仅在填入后使用，请先在游戏中配置对应宏。\n"
            "操作键支持字母、数字、F1–F12、space、enter 等单键。Windows 全局快捷键支持 F1–F12，"
            "留空可关闭；工具打开期间占用这些键。"
        )
        wrapped_label(game, hint, bootstyle="secondary").grid(
            row=5, column=0, columnspan=4, sticky="ew", pady=(8, 10)
        )
        self.save_game = tb.Button(
            game, text="保存游戏设置", command=self._save_game, bootstyle="success"
        )
        self.save_game.grid(row=6, column=0, sticky="w")
        self.game_note = wrapped_label(game)
        self.game_note.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(6, 0))

        vision = tb.Labelframe(f, text="咬钩确认", padding=12)
        vision.pack(fill="x", pady=6)
        self.confirm = tk.BooleanVar(value=presenter.cfg.vision.confirm_bite)
        tb.Checkbutton(
            vision,
            text="结合自己的浮漂画面变化确认咬钩",
            variable=self.confirm,
            bootstyle="success-round-toggle",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self.motion = self._entry(
            vision, "画面变化阈值", presenter.cfg.vision.motion_threshold, 1, 0
        )
        self.confirm_window = self._entry(
            vision, "声音后确认时间（秒）", presenter.cfg.vision.confirm_window_s, 1, 2
        )
        wrapped_label(
            vision,
            "开启后，听到水花还会核对浮漂附近的画面变化，减少旁人水花导致的误收杆。"
            "持续漏掉咬钩时，可先缩小识别范围、降低画面阈值，再用录音验证检查声音。"
            "阈值越高，要求画面变化越明显。",
            bootstyle="secondary",
        ).grid(row=2, column=0, columnspan=4, sticky="ew", pady=10)
        tb.Button(vision, text="保存识别设置", command=self._save_vision, bootstyle="success").grid(
            row=3, column=0, sticky="w"
        )
        self.vision_note = wrapped_label(vision)
        self.vision_note.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(6, 0))

        config = tb.Labelframe(f, text="配置文件", padding=12)
        config.pack(fill="x", pady=6)
        from wowfish import paths

        wrapped_label(config, str(paths.config_file()), bootstyle="secondary").pack(fill="x")
        wrapped_label(
            config,
            "配置无法读取时，可先备份原文件再恢复默认值。自定义字段和注释会在正常保存时保留。",
        ).pack(fill="x", pady=8)
        self.recover = tb.Button(
            config, text="备份并恢复默认配置", command=self._recover, bootstyle="warning"
        )
        self.recover.pack(anchor="w")
        self.config_note = wrapped_label(config)
        self.config_note.pack(fill="x", pady=(6, 0))

    @staticmethod
    def _entry(parent: Any, label: str, value: object, row: int, column: int) -> Any:
        import ttkbootstrap as tb

        tb.Label(parent, text=label).grid(row=row, column=column, sticky="w", pady=6)
        entry = tb.Entry(parent, width=12)
        set_entry(entry, value)
        entry.grid(row=row, column=column + 1, sticky="ew", padx=(8, 16), pady=6)
        return entry

    def _save_game(self) -> None:
        try:
            self.p.save_game_settings(
                cast_key=normalize_key(self.cast.get()),
                interact_key=normalize_key(self.interact.get()),
                lure_key=normalize_key(self.lure.get()),
                quit_key=normalize_key(self.quit.get()),
                session_minutes=int(self.minutes.get()),
                lure_minutes=float(self.lure_minutes.get()),
                window_title=self.title.get().strip(),
                pause_hotkey=normalize_key(self.pause_key.get()),
                stop_hotkey=normalize_key(self.stop_key.get()),
            )
        except Exception as e:
            self.game_note.configure(text=f"保存失败：{error_text(str(e))}", bootstyle="danger")
            return
        self.p.clear_selected_window()
        self.game_note.configure(text="已保存；下一次开始钓鱼时生效。", bootstyle="success")

    def _save_vision(self) -> None:
        try:
            self.p.save_vision_settings(
                confirm_bite=self.confirm.get(),
                motion_threshold=float(self.motion.get()),
                confirm_window_s=float(self.confirm_window.get()),
            )
        except Exception as e:
            self.vision_note.configure(text=f"保存失败：{error_text(str(e))}", bootstyle="danger")
            return
        self.vision_note.configure(text="已保存；下一次开始钓鱼时生效。", bootstyle="success")

    def _recover(self) -> None:
        try:
            if self.p.running or self.p.working:
                raise ValueError("请先停止钓鱼并取消校准任务")
            backup = self.p.restore_config()
            g = self.p.cfg.game
            for entry, value in (
                (self.cast, g.cast_key),
                (self.interact, g.interact_key),
                (self.lure, g.lure_key),
                (self.quit, g.quit_key),
                (self.minutes, g.session_minutes),
                (self.lure_minutes, g.lure_minutes),
                (self.pause_key, g.pause_hotkey.upper()),
                (self.stop_key, g.stop_hotkey.upper()),
                (self.title, g.window_title),
                (self.motion, self.p.cfg.vision.motion_threshold),
                (self.confirm_window, self.p.cfg.vision.confirm_window_s),
            ):
                set_entry(entry, value)
            self.confirm.set(self.p.cfg.vision.confirm_bite)
            self.config_note.configure(
                text=f"已恢复默认配置，原文件备份为：{backup}", bootstyle="success"
            )
        except Exception as e:
            self.config_note.configure(text=error_text(str(e)), bootstyle="danger")

    def render(self, view: View) -> None:
        self.recover.configure(state="disabled" if view.running or view.working else "normal")
