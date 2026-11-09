"""Live mood view, recent changes and local settings."""

from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk

from .chat_view import PAGE, WHITE, INK, MUTED, BLUE, LINE, FONT
from .pet_mood import EMOTIONS, MoodError


MODES = {"持续心情＋变化原因": "event", "只带当前心情": "label", "不影响对话": "off"}
SOURCES = {"user": "聊天", "proactive": "主动联系", "drawing": "自主绘画", "decay": "自然回稳"}


class MoodWindow(tk.Toplevel):
    def __init__(self, master, store, pet_name):
        super().__init__(master)
        self.store = store
        self._closed = False
        self._refresh_job = None
        self._history_key = None
        self.title(f"{pet_name}的心情")
        self.geometry("660x800")
        self.minsize(620, 720)
        self.configure(bg=PAGE)
        if master.winfo_viewable():
            self.transient(master)
        else:
            self.attributes("-topmost", True)
        snapshot = store.snapshot()
        header = tk.Frame(self, bg=WHITE, padx=24, pady=16)
        header.pack(fill="x")
        tk.Label(header, text=f"{pet_name}的心情", bg=WHITE, fg=INK, font=(FONT, 17, "bold")).pack(anchor="w")
        self.mood_text = tk.StringVar(self)
        self.score_label = tk.Label(header, textvariable=self.mood_text, bg=WHITE, fg=BLUE, font=(FONT, 23, "bold"))
        self.score_label.pack(anchor="w", pady=(8, 3))
        self.reason = tk.StringVar(self)
        tk.Label(header, textvariable=self.reason, bg=WHITE, fg=INK, font=(FONT, 10), wraplength=600,
                 justify="left", anchor="w").pack(fill="x")
        self.check = tk.StringVar(self)
        tk.Label(header, textvariable=self.check, bg=WHITE, fg=MUTED, font=(FONT, 9), wraplength=600,
                 justify="left").pack(anchor="w", pady=(6, 0))
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=8)
        overview = tk.Frame(self.tabs, bg=PAGE)
        settings = tk.Frame(self.tabs, bg=PAGE)
        self.tabs.add(overview, text="心情与记录")
        self.tabs.add(settings, text="设置")
        dims = tk.Frame(overview, bg=PAGE, padx=12, pady=10)
        dims.pack(fill="x")
        self.bars, self.dim_values = {}, {}
        self._dim_levels = {key: 0 for key in EMOTIONS}
        for row, (key, label) in enumerate(EMOTIONS.items()):
            tk.Label(dims, text=label, bg=PAGE, fg=INK, font=(FONT, 10), width=5, anchor="w").grid(row=row, column=0, pady=2)
            self.bars[key] = tk.Canvas(dims, height=8, bg=LINE, highlightthickness=0)
            self.bars[key].grid(row=row, column=1, sticky="ew", padx=12)
            self.bars[key].bind("<Configure>", lambda _event, name=key: self._paint_bar(name))
            self.dim_values[key] = tk.StringVar(self)
            tk.Label(dims, textvariable=self.dim_values[key], bg=PAGE, fg=MUTED, font=(FONT, 9), width=5).grid(row=row, column=2)
        dims.columnconfigure(1, weight=1)
        controls = tk.LabelFrame(settings, text="心情设置", bg=PAGE, fg=INK, font=(FONT, 10), padx=12, pady=6)
        controls.pack(fill="x", padx=12, pady=12)
        config = snapshot["config"]
        self.enabled = tk.BooleanVar(self, value=config["enabled"])
        tk.Checkbutton(controls, text="每轮自动判断（额外一次简短模型调用）", variable=self.enabled,
                       bg=PAGE, fg=INK, activebackground=PAGE, font=(FONT, 9)).grid(row=0, column=0, columnspan=4, sticky="w")
        self.mode = tk.StringVar(self, value=next(name for name, value in MODES.items() if value == config["inject_mode"]))
        tk.Label(controls, text="对话中的影响", bg=PAGE, fg=INK, font=(FONT, 9)).grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(controls, textvariable=self.mode, values=list(MODES), state="readonly", width=18).grid(
            row=1, column=1, columnspan=3, sticky="w", padx=(8, 0))
        self.variables = {}
        fields = [("baseline", "平时心情", 30, 100), ("floor", "心情下限", 0, 100),
                  ("half_life_hours", "回稳半衰期/小时", 1, 168), ("confidence", "可信度门槛", 0, 100),
                  ("daily_limit", "每天带入原因/次", 0, 50), ("sensitivity", "反应强度/%", 30, 300)]
        for i, (key, label, low, high) in enumerate(fields):
            row, col = 2 + i // 2, (i % 2) * 2
            tk.Label(controls, text=label, bg=PAGE, fg=INK, font=(FONT, 9)).grid(row=row, column=col, sticky="w", pady=4)
            variable = self.variables[key] = tk.StringVar(self, value=str(config[key]))
            tk.Spinbox(controls, textvariable=variable, from_=low, to=high, width=6, bg=WHITE, fg=INK,
                       relief="flat", font=(FONT, 10)).grid(row=row, column=col + 1, sticky="w", padx=(8, 20))
        tk.Label(controls, text="用户称呼", bg=PAGE, fg=INK, font=(FONT, 9)).grid(row=5, column=0, sticky="w")
        self.owner_name = tk.StringVar(self, value=config["owner_name"])
        tk.Entry(controls, textvariable=self.owner_name, width=10, bg=WHITE, fg=INK, relief="flat",
                 font=(FONT, 10)).grid(row=5, column=1, sticky="w", padx=(8, 0))
        controls.columnconfigure(2, weight=1)
        tk.Label(settings, text="心情总分会慢慢回到平时的状态；七种情绪的半衰期为90分钟。\n\n"
                 "默认反应强度150%，越高波动越明显。当前心情持续影响后续回复；每日次数只限制变化原因的带入，不限制当前心情。\n\n"
                 "低于可信度门槛的变化不带入对话。关闭自动判断后，不再发起判断请求，也不再影响语气和动作活跃程度。\n\n"
                 "心情只是表达的参考，不改变原有人设，也不会因为你暂时没来聊天而扣分。",
                 bg=PAGE, fg=MUTED, font=(FONT, 9), wraplength=570, justify="left").pack(anchor="w", padx=20, pady=12)
        tk.Label(overview, text="最近的心情变化", bg=PAGE, fg=INK, font=(FONT, 10, "bold")).pack(anchor="w", padx=12)
        history_frame = tk.Frame(overview, bg=WHITE)
        history_frame.pack(fill="both", expand=True, padx=12, pady=6)
        scrollbar = ttk.Scrollbar(history_frame)
        scrollbar.pack(side="right", fill="y")
        self.history = tk.Text(history_frame, height=5, wrap="word", bg=WHITE, fg=INK,
                               relief="flat", padx=10, pady=8, font=(FONT, 9), yscrollcommand=scrollbar.set)
        self.history.pack(fill="both", expand=True)
        scrollbar.configure(command=self.history.yview)
        footer = tk.Frame(self, bg=WHITE, padx=24, pady=12)
        footer.pack(fill="x", side="bottom", before=self.tabs)
        tk.Button(footer, text="重置到平时心情", command=self._reset, bg=WHITE, fg=MUTED, relief="flat",
                  font=(FONT, 9), padx=8, pady=6).pack(side="left")
        self.notice = tk.StringVar(self)
        tk.Label(footer, textvariable=self.notice, bg=WHITE, fg=MUTED, font=(FONT, 9)).pack(side="left", padx=8)
        tk.Button(footer, text="保存设置", command=self._save, bg=BLUE, fg=WHITE, relief="flat",
                  font=(FONT, 10), padx=16, pady=7, cursor="hand2").pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._refresh()

    def _paint_bar(self, key):
        bar = self.bars[key]
        bar.delete("all")
        width = max(1, bar.winfo_width()) * self._dim_levels[key] / 100
        bar.create_rectangle(0, 0, width, 8, fill=BLUE, outline="")

    def _refresh(self):
        self._refresh_job = None
        if self._closed:
            return
        try:
            snapshot = self.store.snapshot()
            self.mood_text.set(f"{snapshot['label']}  {snapshot['mood']:.1f} / 100")
            self.score_label.configure(fg=snapshot["color"])
            latest = snapshot["latest"]
            text = latest["reason"] if latest else "心情会随着聊天与自主活动慢慢变化，也会随时间回稳。"
            self.reason.set(text[:90] + ("…" if len(text) > 90 else ""))
            check = snapshot["last_check"]
            text = "每轮回复发送后在后台判断，不等待判断完成才显示回复。"
            if not snapshot["config"]["enabled"]:
                text = "自动判断已暂停；当前心情仍会自然回稳。"
            elif check:
                status = {"changed": "已更新心情", "unchanged": "这一轮没有明显变化",
                          "failed": "上次判断未成功，正常聊天不受影响", "skipped": "待判断较多，这一轮已跳过"}[check["status"]]
                text = datetime.fromtimestamp(check["at"]).strftime("%H:%M") + " · " + status
            self.check.set(text)
            for key in EMOTIONS:
                self._dim_levels[key] = snapshot["dims"][key]
                self._paint_bar(key)
                self.dim_values[key].set(f"{snapshot['dims'][key]:.1f}")
            history_key = tuple(entry["id"] for entry in snapshot["history"])
            if history_key != self._history_key:
                self._history_key = history_key
                lines = []
                for entry in reversed(snapshot["history"]):
                    stamp = datetime.fromtimestamp(entry["at"]).strftime("%m-%d %H:%M")
                    low = " · 仅记录" if entry["confidence"] < snapshot["config"]["confidence"] else ""
                    lines.append(f"{stamp}  {SOURCES[entry['source']]}  {entry['mood']:.1f} ({entry['delta']:+.1f}){low}\n{entry['reason']}\n")
                self.history.configure(state="normal")
                self.history.delete("1.0", "end")
                self.history.insert("1.0", "\n".join(lines) or "还没有心情变化记录。")
                self.history.configure(state="disabled")
        except (MoodError, OSError):
            self.check.set("暂时无法读取心情，请检查文件是否可读写。")
        self._refresh_job = self.after(2000, self._refresh)

    def _save(self):
        try:
            config = {key: int(variable.get()) for key, variable in self.variables.items()}
            config.update(enabled=self.enabled.get(), inject_mode=MODES[self.mode.get()], owner_name=self.owner_name.get())
            self.store.save_config(config)
            self.notice.set("已保存，立即生效")
            if self._refresh_job is not None:
                self.after_cancel(self._refresh_job)
            self._history_key = None
            self._refresh()
        except (MoodError, OSError, ValueError) as exc:
            messagebox.showerror("无法保存心情设置", str(exc), parent=self)

    def _reset(self):
        if not messagebox.askyesno("重置心情", "回到平时心情，并清空这份心情时间线？聊天记录不会受影响。", parent=self):
            return
        try:
            self.store.reset()
            self.notice.set("已重置")
            if self._refresh_job is not None:
                self.after_cancel(self._refresh_job)
            self._refresh()
        except (MoodError, OSError) as exc:
            messagebox.showerror("无法重置心情", str(exc), parent=self)

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        if self._refresh_job is not None:
            self.after_cancel(self._refresh_job)
            self._refresh_job = None
        super().destroy()
