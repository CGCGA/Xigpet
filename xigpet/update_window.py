"""Small progress window shared by chat, desktop and tray update entries."""

import tkinter as tk
from tkinter import ttk

from .chat_view import PAGE, WHITE, INK, MUTED, BLUE, FONT
from .version import VERSION


class UpdateWindow(tk.Toplevel):
    def __init__(self, master, cancel):
        super().__init__(master)
        self.title("检查更新")
        self.geometry("460x245")
        self.resizable(False, False)
        self.configure(bg=PAGE)
        if master.winfo_viewable():
            self.transient(master)
        else:
            self.attributes("-topmost", True)
        self.cancel = cancel
        tk.Label(self, text="软件更新", font=(FONT, 16, "bold"), bg=PAGE, fg=INK).pack(anchor="w", padx=24, pady=(20, 5))
        tk.Label(self, text=f"当前版本 {VERSION}", font=(FONT, 10), bg=PAGE, fg=MUTED).pack(anchor="w", padx=24)
        self.text = tk.StringVar(self, value="正在检查 GitHub 上的新版本…")
        tk.Label(self, textvariable=self.text, font=(FONT, 10), bg=PAGE, fg=INK,
                 wraplength=410, justify="left").pack(anchor="w", padx=24, pady=14)
        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", padx=24)
        self.progress.start(25)
        self.button = tk.Button(self, text="取消", command=self._close, relief="flat", bg=WHITE, fg=BLUE,
                                font=(FONT, 10), padx=16, pady=7)
        self.button.pack(side="right", padx=24, pady=15)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def show_status(self, text, *, finished=False):
        self.text.set(text)
        if finished:
            self.progress.stop()
            self.button.configure(text="关闭")

    def _close(self):
        self.cancel()
        self.destroy()

    def destroy(self):
        self.progress.stop()
        super().destroy()
