"""Owner controls for the pet's editable persona supplement."""
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox

from .chat_view import PAGE, WHITE, INK, MUTED, BLUE, FONT
from .self_growth import FIELDS, GrowthError
from .pet_mood import MoodError


class GrowthWindow(tk.Toplevel):
    def __init__(self, master, store):
        super().__init__(master)
        self.store = store
        self.title("自我成长")
        self.geometry("690x760")
        self.minsize(620, 680)
        self.configure(bg=PAGE)
        tk.Label(self, text="宠物的成长空间", bg=PAGE, fg=INK, font=(FONT, 17, "bold")).pack(anchor="w", padx=20, pady=(16, 4))
        tk.Label(self, text="主人设始终优先。宠物只维护下面四项；锁定后仅你能修改。", bg=PAGE,
                 fg=MUTED, font=(FONT, 10)).pack(anchor="w", padx=20, pady=(0, 10))
        footer = tk.Frame(self, bg=WHITE, padx=16, pady=10)
        footer.pack(side="bottom", fill="x")
        self.notice = tk.StringVar(self)
        tk.Label(footer, textvariable=self.notice, bg=WHITE, fg=MUTED).pack(side="left")
        tk.Button(footer, text="保存", command=self._save, bg=BLUE, fg=WHITE, relief="flat", padx=18).pack(side="right")
        tk.Button(footer, text="重新读取", command=self._reload, bg=WHITE, relief="flat").pack(side="right", padx=8)
        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        editor, history = tk.Frame(tabs, bg=PAGE), tk.Frame(tabs, bg=PAGE)
        tabs.add(editor, text="内容与权限")
        tabs.add(history, text="修改记录")
        self.enabled = tk.BooleanVar(self)
        tk.Checkbutton(editor, text="允许宠物自主修改开放字段", variable=self.enabled, bg=PAGE).pack(anchor="w", padx=10, pady=6)
        controls = tk.Frame(editor, bg=PAGE)
        controls.pack(fill="x", padx=10)
        self.cooldown, self.daily_limit = tk.StringVar(self), tk.StringVar(self)
        for text, variable, high in (("最短间隔/小时", self.cooldown, 72), ("每天最多修改/次", self.daily_limit, 12)):
            tk.Label(controls, text=text, bg=PAGE).pack(side="left", padx=(0, 6))
            tk.Spinbox(controls, textvariable=variable, from_=0, to=high, width=4).pack(side="left", padx=(0, 18))
        self.editors, self.locks = {}, {}
        for key, label in FIELDS.items():
            frame = tk.LabelFrame(editor, text=label + "（最多300字）", bg=PAGE, fg=INK, padx=8, pady=4)
            frame.pack(fill="both", expand=True, padx=10, pady=5)
            self.locks[key] = tk.BooleanVar(self)
            tk.Checkbutton(frame, text="锁定", variable=self.locks[key], bg=PAGE).pack(side="right", anchor="n")
            text = self.editors[key] = tk.Text(frame, height=3, wrap="word", font=(FONT, 10), bg=WHITE, relief="flat")
            text.pack(fill="both", expand=True)
        tk.Label(history, text="选中一条记录后，可恢复到该次修改之前的完整成长设定。", bg=PAGE, fg=MUTED,
                 wraplength=600, justify="left").pack(anchor="w", padx=10, pady=10)
        tk.Button(history, text="恢复到所选修改之前", command=self._rollback).pack(anchor="w", padx=10)
        self.history = tk.Listbox(history, height=10, exportselection=False)
        self.history.pack(fill="both", expand=True, padx=10, pady=10)
        self.detail = tk.Text(history, height=10, wrap="word", state="disabled", font=(FONT, 10))
        self.detail.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.history.bind("<<ListboxSelect>>", self._show_detail)
        self._reload()

    def _reload(self):
        try:
            self.data = self.store.snapshot()
            self.revision = self.data["revision"]
            config = self.data["config"]
            self.enabled.set(config["enabled"])
            self.cooldown.set(str(config["cooldown_hours"]))
            self.daily_limit.set(str(config["daily_limit"]))
            for key in FIELDS:
                self.editors[key].delete("1.0", "end")
                self.editors[key].insert("1.0", self.data["fields"][key])
                self.locks[key].set(key in config["locked_fields"])
            self.rows = list(reversed(self.data["history"]))
            self.history.delete(0, "end")
            for row in self.rows:
                actor = "宠物" if row["actor"] == "pet" else "主人"
                self.history.insert("end", f"{datetime.fromtimestamp(row['at']):%m-%d %H:%M} · {actor} · {row['reason']}")
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.configure(state="disabled")
        except (GrowthError, MoodError, OSError) as exc:
            messagebox.showerror("成长设定暂不可用", str(exc), parent=self)

    def _show_detail(self, _event=None):
        selected = self.history.curselection()
        if not selected:
            return
        row = self.rows[selected[0]]
        lines = [row["reason"]]
        for key, label in FIELDS.items():
            if row["before"][key] != row["after"][key]:
                lines.append(f"\n{label}\n之前：{row['before'][key] or '空'}\n之后：{row['after'][key] or '空'}")
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "\n".join(lines))
        self.detail.configure(state="disabled")

    def _save(self):
        try:
            config = {"enabled": self.enabled.get(), "locked_fields": [k for k in FIELDS if self.locks[k].get()],
                      "cooldown_hours": int(self.cooldown.get()), "daily_limit": int(self.daily_limit.get())}
            self.store._validate_config(config)
            self.store.update({key: widget.get("1.0", "end-1c") for key, widget in self.editors.items()},
                              self.revision, "主人编辑成长设定。", actor="owner")
            self.store.save_config(config)
            self._reload()
            self.notice.set("已保存，后续对话生效")
        except (GrowthError, MoodError, OSError, ValueError) as exc:
            messagebox.showerror("无法保存", str(exc), parent=self)

    def _rollback(self):
        selected = self.history.curselection()
        if not selected:
            self.notice.set("请先选择一条修改记录")
            return
        try:
            self.store.rollback(self.rows[selected[0]]["id"], self.revision)
            self._reload()
            self.notice.set("已恢复，恢复操作也保留记录")
        except (GrowthError, MoodError, OSError) as exc:
            messagebox.showerror("无法恢复", str(exc), parent=self)
