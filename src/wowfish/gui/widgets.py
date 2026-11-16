"""Small Tk helpers for the Chinese desktop pages."""

from __future__ import annotations

from typing import Any


class ScrollFrame:
    """Keep all settings reachable in a small window and at larger font sizes."""

    def __init__(self, parent: Any) -> None:
        import tkinter as tk

        import ttkbootstrap as tb

        self.frame = tb.Frame(parent)
        self.canvas = tk.Canvas(self.frame, highlightthickness=0, background="#212529")
        bar = tb.Scrollbar(self.frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = tb.Frame(self.canvas, padding=16)
        self._window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._content_changed)
        self.canvas.bind("<Configure>", self._canvas_changed)
        # Tk sends wheel events to the widget under the pointer, including nested children.
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.frame.bind_all(event, self._wheel, add="+")

    def _content_changed(self, _event: Any) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_changed(self, event: Any) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _wheel(self, event: Any) -> None:
        widget = event.widget
        while widget is not None and widget is not self.frame:
            widget = getattr(widget, "master", None)
        if widget is None or self.canvas.yview() == (0.0, 1.0):
            return
        number = getattr(event, "num", None)
        delta = -1 if number == 4 else 1 if number == 5 else -int(event.delta / 120)
        if not delta and getattr(event, "delta", 0):
            delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta * 3, "units")


def wrapped_label(parent: Any, text: str = "", **kwargs: Any) -> Any:
    import ttkbootstrap as tb

    label = tb.Label(parent, text=text, justify="left", wraplength=760, **kwargs)
    parent.bind(
        "<Configure>",
        lambda event: label.configure(wraplength=max(160, event.width - 40)),
        add="+",
    )
    return label


def set_entry(entry: Any, value: object) -> None:
    entry.delete(0, "end")
    entry.insert(0, str(value))


def copy_text(widget: Any, text: str) -> None:
    widget.clipboard_clear()
    widget.clipboard_append(text)
