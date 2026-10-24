"""Search source messages and inspect the bounded context/summary settings."""

import json
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from .chat_view import PAGE, WHITE, INK, MUTED, BLUE, LINE, FONT
from .memory_index import MemoryError


class MemoryWindow(tk.Toplevel):
    def __init__(self, master, memory):
        super().__init__(master)
        self.memory = memory
        self._closed = self._busy = False
        self._results = queue.Queue(maxsize=4)
        self._poll_job = None
        self._last_stats = time.monotonic()
        self._hits = []
        self._selection = None
        self._next = None
        self._decompress = False
        self._pending_selection = False
        self.title("记忆与上下文")
        self.geometry("800x760")
        self.minsize(700, 650)
        self.configure(bg=PAGE)
        if master.winfo_viewable():
            self.transient(master)
        else:
            self.attributes("-topmost", True)
        header = tk.Frame(self, bg=WHITE, padx=24, pady=15)
        header.pack(fill="x")
        tk.Label(header, text="记忆与上下文", bg=WHITE, fg=INK, font=(FONT, 17, "bold")).pack(anchor="w")
        self.status = tk.StringVar(self, value="正在同步本地索引…")
        tk.Label(header, textvariable=self.status, bg=WHITE, fg=MUTED, font=(FONT, 9), wraplength=730,
                 justify="left").pack(anchor="w", pady=(6, 0))
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=16, pady=12)
        search = tk.Frame(self.tabs, bg=PAGE, padx=10, pady=10)
        settings = tk.Frame(self.tabs, bg=PAGE, padx=20, pady=18)
        self.tabs.add(search, text="检索与原文")
        self.tabs.add(settings, text="上下文设置")
        form = tk.Frame(search, bg=PAGE)
        form.pack(fill="x")
        self.query = tk.StringVar(self)
        entry = tk.Entry(form, textvariable=self.query, bg=WHITE, fg=INK, font=(FONT, 11), relief="flat")
        entry.pack(side="left", fill="x", expand=True, ipady=7)
        entry.bind("<Return>", lambda _event: self._search())
        self.search_button = tk.Button(form, text="搜索", command=self._search, bg=BLUE, fg=WHITE,
                                        relief="flat", font=(FONT, 10), padx=18, pady=6)
        self.search_button.pack(side="left", padx=(10, 0))
        filters = tk.Frame(search, bg=PAGE)
        filters.pack(fill="x", pady=8)
        self.role = tk.StringVar(self, value="全部来源")
        ttk.Combobox(filters, textvariable=self.role, values=["全部来源", "用户原话", "宠物原话"], state="readonly", width=12).pack(side="left")
        self.date_from, self.date_to = tk.StringVar(self), tk.StringVar(self)
        for label, variable in (("从", self.date_from), ("至", self.date_to)):
            tk.Label(filters, text=label, bg=PAGE, fg=MUTED, font=(FONT, 9)).pack(side="left", padx=6)
            tk.Entry(filters, textvariable=variable, width=11, font=(FONT, 9), relief="flat").pack(side="left")
        tk.Label(filters, text="日期可留空：YYYY-MM-DD", bg=PAGE, fg=MUTED, font=(FONT, 8)).pack(side="left", padx=10)
        self.matches = tk.StringVar(self, value="可查旧对话、重要记忆和压缩摘要；摘要可继续查看对应原文。")
        tk.Label(search, textvariable=self.matches, bg=PAGE, fg=MUTED, font=(FONT, 9), wraplength=720,
                 justify="left").pack(anchor="w", pady=(0, 6))
        table = tk.Frame(search, bg=PAGE)
        table.pack(fill="x")
        self.tree = ttk.Treeview(table, columns=("source", "time", "excerpt"), show="headings", height=6)
        for name, label, width in (("source", "来源", 90), ("time", "日期", 115), ("excerpt", "匹配片段", 460)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=60, stretch=name == "excerpt")
        table_scroll = ttk.Scrollbar(table, command=self.tree.yview)
        table_scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=table_scroll.set)
        self.tree.pack(side="left", fill="x", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        buttons = tk.Frame(search, bg=PAGE)
        buttons.pack(fill="x", pady=8)
        self.source_button = tk.Button(buttons, text="查看摘要来源", command=self._sources, bg=WHITE, fg=BLUE,
                                       relief="flat", font=(FONT, 9), state="disabled")
        self.source_button.pack(side="left")
        self.next_button = tk.Button(buttons, text="继续读取", command=self._continue, bg=WHITE, fg=BLUE,
                                     relief="flat", font=(FONT, 9), state="disabled")
        self.next_button.pack(side="left", padx=10)
        detail_frame = tk.Frame(search, bg=WHITE)
        detail_frame.pack(fill="both", expand=True)
        detail_scroll = ttk.Scrollbar(detail_frame)
        detail_scroll.pack(side="right", fill="y")
        self.details = tk.Text(detail_frame, height=6, wrap="word", bg=WHITE, fg=INK, relief="flat",
                               font=(FONT, 10), padx=12, pady=10)
        self.details.configure(yscrollcommand=detail_scroll.set)
        detail_scroll.configure(command=self.details.yview)
        self.details.pack(fill="both", expand=True)
        self._show_text("检索结果会显示来源角色和时间，点击后读取有校验的原文。聊天文件不会被压缩摘要覆盖。")
        config = memory.settings()
        self.auto = tk.BooleanVar(self, value=config["auto_compress"])
        tk.Checkbutton(settings, text="积累旧对话后自动整理摘要（会额外调用模型）", variable=self.auto,
                       bg=PAGE, fg=INK, activebackground=PAGE, font=(FONT, 10)).pack(anchor="w")
        grid = tk.Frame(settings, bg=PAGE)
        grid.pack(fill="x", pady=14)
        self.variables = {}
        for row, (name, label, low, high, step) in enumerate((
            ("context_budget_tokens", "输入上下文预算 / 估算token", 8000, 96000, 1000),
            ("recent_messages", "优先保留的近期原文条数", 8, 32, 1),
            ("summary_trigger_tokens", "旧对话摘要触发量 / 估算token", 1000, 12000, 500),
        )):
            tk.Label(grid, text=label, bg=PAGE, fg=INK, font=(FONT, 10)).grid(row=row, column=0, sticky="w", pady=10)
            variable = self.variables[name] = tk.StringVar(self, value=str(config[name]))
            tk.Spinbox(grid, from_=low, to=high, increment=step, textvariable=variable, width=10,
                       font=(FONT, 10), relief="flat").grid(row=row, column=1, padx=20)
        tk.Label(settings, text="全文检索在本地执行，不需要额外向量模型。\n\n"
                 "后台摘要只整理达到门槛的一段较早对话；启动时不会把全部旧记录逐段送去调用模型。摘要只是线索，细节仍可查原文。\n\n"
                 "输入预算包括文字、工具说明和图片的保守估算，不等于服务商的实际计费token。超过预算时，较早上下文会存入可读取的本地缓存，当前输入、必要系统说明和未完成的工具调用保留。",
                 bg=PAGE, fg=MUTED, font=(FONT, 10), wraplength=690, justify="left").pack(anchor="w", pady=10)
        self.summary_status = tk.StringVar(self)
        tk.Label(settings, textvariable=self.summary_status, bg=PAGE, fg=BLUE, font=(FONT, 9),
                 wraplength=690, justify="left").pack(anchor="w", pady=10)
        footer = tk.Frame(self, bg=WHITE, padx=20, pady=12)
        footer.pack(fill="x", side="bottom", before=self.tabs)
        self.refresh_button = tk.Button(footer, text="刷新本地索引", command=self._sync, bg=WHITE, fg=BLUE,
                                        font=(FONT, 9), relief="flat", padx=10, pady=7)
        self.refresh_button.pack(side="left")
        tk.Button(footer, text="保存设置", command=self._save, bg=BLUE, fg=WHITE,
                  font=(FONT, 10), relief="flat", padx=18, pady=7).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._sync()
        self._poll()

    def _show_text(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    @staticmethod
    def _format_detail(value):
        if "rows" in value:
            pieces = []
            for row in value["rows"]:
                role = "用户原话" if row["role"] == "user" else "宠物原话"
                pieces.append(f"第{row['line']}行 · {role} · {row.get('time', '')}\n{row['content']}" +
                              ("\n（这条消息较长，可按引用继续读取。）" if row.get("truncated") else ""))
            return "\n\n".join(pieces)
        if value.get("kind") == "summary":
            try:
                summary = json.loads(value["content"])
                parts = ["压缩摘要 · 原文第" + str(value["start_line"]) + "–" + str(value["end_line"]) + "行",
                         summary["title"], summary["summary"]]
                for item in summary.get("facts", []):
                    parts.append(f"第{item['source_line']}行原话：{item['quote']}")
                parts.append("摘要是线索，点击“查看摘要来源”核对完整原文。")
                return "\n\n".join(parts)
            except (ValueError, KeyError):
                return value["content"]
        role = {"user": "用户原话", "assistant": "宠物原话"}.get(value.get("role"), "重要记忆原文")
        return f"{role} · 第{value.get('line', '')}行 · {value.get('time', '')}\n\n{value['content']}"

    def _start(self, kind, operation):
        if self._busy or self._closed:
            return
        self._busy = True
        self.search_button.configure(state="disabled")
        self.refresh_button.configure(state="disabled")
        self.source_button.configure(state="disabled")
        self.next_button.configure(state="disabled")
        def run():
            try:
                value = operation()
                result = kind, value, None
            except Exception as exc:
                result = kind, None, str(exc)
            if not self._closed:
                self._results.put_nowait(result)
        threading.Thread(target=run, name="pet-memory-view", daemon=True).start()

    def _sync(self):
        def sync():
            self.memory.index.sync()
            return self.memory.index.stats()
        self._start("stats", sync)

    def _search(self):
        query = self.query.get().strip()
        if not query:
            return
        role = {"用户原话": "user", "宠物原话": "assistant"}.get(self.role.get())
        first, last = self.date_from.get().strip() or None, self.date_to.get().strip() or None
        self._start("hits", lambda: self.memory.index.search(query, limit=20, role=role, date_from=first, date_to=last))

    def _selected(self, _event=None):
        if self._busy:
            self._pending_selection = True
            return
        if not self.tree.selection():
            return
        hit = self._hits[int(self.tree.selection()[0])]
        self._selection, self._next, self._decompress = hit, None, False
        self.source_button.configure(state="normal" if hit["block_id"] else "disabled")
        self._start("detail", lambda: self.memory.index.read(hit["ref"], offset=max(0, hit["offset"] - 200)))

    def _sources(self):
        if not self._busy and self._selection and self._selection["block_id"]:
            self._decompress = True
            self._start("detail", lambda: self.memory.index.decompress(self._selection["block_id"]))

    def _continue(self):
        if self._busy or self._selection is None or self._next is None:
            return
        if self._decompress:
            self._start("detail", lambda: self.memory.index.decompress(self._selection["block_id"], offset=self._next))
        else:
            self._start("detail", lambda: self.memory.index.read(self._selection["ref"], offset=self._next))

    def _save(self):
        try:
            config = {key: int(variable.get()) for key, variable in self.variables.items()}
            self.memory.save_settings({**config, "auto_compress": self.auto.get()})
            self.summary_status.set("设置已保存，立即生效。")
        except (MemoryError, OSError, ValueError) as exc:
            messagebox.showerror("无法保存记忆设置", str(exc), parent=self)

    def _poll(self):
        self._poll_job = None
        if self._closed:
            return
        try:
            while True:
                kind, value, error = self._results.get_nowait()
                self._busy = False
                self.search_button.configure(state="normal")
                self.refresh_button.configure(state="normal")
                self.source_button.configure(state="normal" if self._selection and self._selection["block_id"] else "disabled")
                self.next_button.configure(state="normal" if self._next is not None else "disabled")
                if error:
                    self.matches.set(error)
                    continue
                if kind == "stats":
                    self._last_stats = time.monotonic()
                    request = value["last_request"]
                    self.status.set(f"已索引 {value['messages']} 条对话 · 已整理 {value['blocks']} 段摘要 · 原始聊天保留")
                    text = value["status"]
                    if request:
                        text += f"\n最近输入估算：{request['before_tokens']} → {request['after_tokens']} token；预算 {request['budget']}。"
                    self.summary_status.set(text)
                elif kind == "hits":
                    self._hits, self._selection, self._next = value, None, None
                    self.tree.delete(*self.tree.get_children())
                    for i, hit in enumerate(value):
                        source = {"user": "用户原话", "assistant": "宠物原话", "memory": "重要记忆", "summary": "压缩摘要"}[hit["role"]]
                        excerpt = hit["excerpt"].split("\n{", 1)[0] if hit["kind"] == "summary" else hit["excerpt"]
                        self.tree.insert("", "end", iid=str(i), values=(source, hit["time"][:10], " ".join(excerpt.split())[:160]))
                    self.matches.set(f"找到 {len(value)} 条相关结果；点击读取原文。" if value else "没有找到相关内容，可换一个具体词或调整日期范围。")
                    self.next_button.configure(state="disabled")
                    self.source_button.configure(state="disabled")
                else:
                    self._next = value.get("next_offset")
                    self.next_button.configure(state="normal" if self._next is not None else "disabled")
                    self._show_text(self._format_detail(value))
        except queue.Empty:
            pass
        if self._pending_selection and not self._busy:
            self._pending_selection = False
            self._selected()
        if not self._busy and time.monotonic() - self._last_stats >= 5:
            self._start("stats", self.memory.index.stats)
        self._poll_job = self.after(150, self._poll)

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
            self._poll_job = None
        super().destroy()
