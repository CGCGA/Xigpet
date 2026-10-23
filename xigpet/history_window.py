"""Read-only, date-based history UI; file indexing and reads stay off the Tk thread."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import date
from tkinter import ttk

from .chat_history import HistoryArchive, HISTORY_PAGE_SIZE, UNKNOWN_DATE
from .chat_view import ConversationFrame, PAGE, WHITE, INK, MUTED, LINE, BLUE, FONT


class HistoryWindow(tk.Toplevel):
    def __init__(self, master, catalog, pet_name):
        super().__init__(master)
        self.title(f"{pet_name} · 聊天记录")
        self.geometry("880x700")
        self.minsize(640, 480)
        self.configure(bg=PAGE)
        self.transient(master)
        self.archive = HistoryArchive(catalog)
        self._results = queue.Queue()
        self._request_id = 0
        self._poll_job = None
        self._closed = False
        self._loading = False
        self._dates = []
        self.day = None
        self.page_index = 0
        self.total = 0

        header = tk.Frame(self, bg=WHITE, padx=22, pady=16)
        header.pack(fill="x")
        self.back_button = self._button(header, "‹ 日期列表", self.show_dates)
        self.heading = tk.StringVar(value="聊天记录")
        self.heading_label = tk.Label(header, textvariable=self.heading, bg=WHITE, fg=INK,
                                      font=(FONT, 16, "bold"))
        self.heading_label.pack(side="left")
        self._button(header, "刷新", self.refresh).pack(side="right")
        tk.Frame(self, bg=LINE, height=1).pack(fill="x")
        self.status = tk.StringVar(value="正在读取记录…")
        tk.Label(self, textvariable=self.status, bg=PAGE, fg=MUTED,
                 font=(FONT, 9), anchor="w").pack(fill="x", padx=24, pady=(12, 6))

        self.dates_frame = tk.Frame(self, bg=PAGE, padx=24, pady=8)
        self.dates_frame.pack(fill="both", expand=True)
        search = tk.Frame(self.dates_frame, bg=PAGE)
        search.pack(fill="x", pady=(0, 12))
        tk.Label(search, text="查找日期", bg=PAGE, fg=INK, font=(FONT, 10)).pack(side="left", padx=(0, 12))
        self.date_filter = tk.StringVar()
        self.search_entry = tk.Entry(search, textvariable=self.date_filter, bg=WHITE, fg=INK,
                                     relief="flat", font=(FONT, 11), width=18,
                                     highlightthickness=1, highlightbackground=LINE,
                                     highlightcolor=BLUE)
        self.search_entry.pack(side="left", ipady=6)
        tk.Label(search, text="例如 2026-10 或 2026-10-22", bg=PAGE, fg=MUTED,
                 font=(FONT, 9)).pack(side="left", padx=12)
        self.date_filter.trace_add("write", lambda *_: self._render_dates())
        style = ttk.Style(self)
        style.configure("History.Treeview", background=WHITE, fieldbackground=WHITE,
                        foreground=INK, rowheight=48, borderwidth=0, font=(FONT, 11))
        style.configure("History.Treeview.Heading", font=(FONT, 10, "bold"), foreground=INK)
        style.map("History.Treeview", background=[("selected", "#ddecfa")],
                  foreground=[("selected", INK)])
        table = tk.Frame(self.dates_frame, bg=WHITE)
        table.pack(fill="both", expand=True)
        self.date_list = ttk.Treeview(table, columns=("date", "count"), show="headings",
                                     selectmode="browse", style="History.Treeview")
        self.date_list.heading("date", text="日期", anchor="w")
        self.date_list.heading("count", text="消息", anchor="e")
        self.date_list.column("date", width=440, anchor="w")
        self.date_list.column("count", width=120, anchor="e", stretch=False)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.date_list.yview)
        self.date_list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.date_list.pack(side="left", fill="both", expand=True)
        self.date_list.bind("<Double-1>", lambda _event: self.open_selected())
        self.date_list.bind("<Return>", lambda _event: self.open_selected())
        self._button(self.dates_frame, "查看当天聊天  →", self.open_selected).pack(anchor="e", pady=14)

        self.day_frame = tk.Frame(self, bg=PAGE)
        footer = tk.Frame(self.day_frame, bg=WHITE, padx=22, pady=12)
        footer.pack(side="bottom", fill="x")
        self.page_label = tk.StringVar()
        tk.Label(footer, textvariable=self.page_label, bg=WHITE, fg=MUTED,
                 font=(FONT, 9)).pack(side="left")
        self.next_button = self._button(footer, "下一页 ›", lambda: self.load_day(self.day, self.page_index + 1))
        self.next_button.pack(side="right")
        self.previous_button = self._button(footer, "‹ 上一页", lambda: self.load_day(self.day, self.page_index - 1))
        self.previous_button.pack(side="right", padx=10)
        self.conversation = ConversationFrame(self.day_frame, pet_name)
        self.conversation.pack(fill="both", expand=True)
        self.conversation._background = True
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.refresh()

    @staticmethod
    def _button(parent, text, command):
        return tk.Button(parent, text=text, command=command, bg=WHITE, fg=BLUE,
                         activebackground=PAGE, relief="flat", bd=0, cursor="hand2",
                         font=(FONT, 10), padx=12, pady=7)

    def _load(self, kind, operation):
        self._request_id += 1
        request_id = self._request_id
        self._loading = True
        self.status.set("正在读取记录…")
        self.previous_button.configure(state="disabled")
        self.next_button.configure(state="disabled")
        results = self._results
        def worker():
            try:
                results.put((request_id, kind, operation(), None))
            except Exception as exc:
                results.put((request_id, kind, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()
        if self._poll_job is None:
            self._poll_job = self.after(80, self._poll_results)

    def _poll_results(self):
        self._poll_job = None
        if self._closed:
            return
        while True:
            try:
                request_id, kind, result, error = self._results.get_nowait()
            except queue.Empty:
                break
            if request_id != self._request_id:
                continue
            self._loading = False
            if error:
                self.status.set(f"读取失败，可点击刷新重试：{error}")
                continue
            if kind == "dates":
                self._dates = result
                self._render_dates()
            else:
                records, self.total, self.page_index = result
                self.conversation.set_messages(records)
                self.status.set(f"{self.day} 的聊天 · 只读查看")
                pages = max(1, (self.total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
                self.page_label.set(f"第 {self.page_index + 1} / {pages} 页 · 共 {self.total} 条")
                self.previous_button.configure(state="normal" if self.page_index else "disabled")
                self.next_button.configure(state="normal" if self.page_index + 1 < pages else "disabled")
        if self._loading:
            self._poll_job = self.after(80, self._poll_results)

    def _render_dates(self):
        self.date_list.delete(*self.date_list.get_children())
        query = self.date_filter.get().strip()
        matches = [(day, count) for day, count in self._dates if query in day]
        weekdays = "一二三四五六日"
        for day, count in matches:
            label = day if day == UNKNOWN_DATE else f"{day}   周{weekdays[date.fromisoformat(day).weekday()]}"
            self.date_list.insert("", "end", iid=day, values=(label, f"{count} 条"))
        children = self.date_list.get_children()
        if children:
            self.date_list.selection_set(children[0])
        if self.day is None and not self._loading:
            self.status.set(f"共 {len(self._dates)} 天 · 选择日期查看聊天" if matches else
                            ("没有匹配的日期" if self._dates else "还没有聊天记录"))

    def refresh(self):
        if self.day is None:
            self._load("dates", self.archive.dates)
        else:
            self.load_day(self.day, self.page_index)

    def open_selected(self):
        selected = self.date_list.selection()
        if selected and not self._loading:
            self.load_day(selected[0], 0)

    def load_day(self, day, page=0):
        self.day = day
        self.heading.set(day)
        self.back_button.pack(side="left", before=self.heading_label)
        self.dates_frame.pack_forget()
        self.day_frame.pack(fill="both", expand=True)
        self.conversation._background = False
        self.conversation.clear_messages()
        self._load("day", lambda archive=self.archive: archive.page(day, page))

    def show_dates(self):
        self.day = None
        self.heading.set("聊天记录")
        self.back_button.pack_forget()
        self.day_frame.pack_forget()
        self.dates_frame.pack(fill="both", expand=True)
        self.conversation._background = True
        self.conversation.clear_messages()
        self.refresh()

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        self._loading = False
        self._request_id += 1
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
            self._poll_job = None
        self.conversation.destroy()
        self.archive = None  # A closed window must not retain the date index in the background.
        self._dates.clear()
        super().destroy()
        # Do not leave Tk objects in cycles that a later worker thread may collect.
        for name, value in tuple(vars(self).items()):
            if isinstance(value, (tk.Misc, tk.Variable)):
                setattr(self, name, None)
