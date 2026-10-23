"""QQ mode setup, opened from the desktop pet's context menu."""
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

from .chat_view import PAGE, WHITE, INK, MUTED, BLUE, LINE, FONT
from .qq_bridge import QQSettings, QQError, import_snowluma


class QQSettingsWindow(tk.Toplevel):
    def __init__(self, master, path, snow_folder, on_enable, on_disable, status, enabled):
        super().__init__(master)
        self.title("QQ 接入模式")
        self.geometry("650x620")
        self.minsize(600, 600)
        self.configure(bg=PAGE)
        if master.winfo_viewable():
            self.transient(master)
        else:
            self.attributes("-topmost", True)
        self.path, self.snow_folder = Path(path), Path(snow_folder)
        self.on_enable, self.on_disable = on_enable, on_disable
        self.settings = QQSettings.load(path)
        self._closed = False
        self._import_pending = False
        header = tk.Frame(self, bg=WHITE, padx=24, pady=18)
        header.pack(fill="x")
        tk.Label(header, text="QQ 接入", bg=WHITE, fg=INK, font=(FONT, 17, "bold")).pack(anchor="w")
        tk.Label(header, text="与指定的 QQ 私聊同步，保留同一份聊天记录", bg=WHITE,
                 fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=(5, 0))
        body = tk.Frame(self, bg=PAGE, padx=24, pady=16)
        body.pack(fill="both", expand=True)
        self.variables = {}
        for row, (key, label) in enumerate((("target_qq", "用户 QQ（收消息的人）"),
                                          ("expected_self_qq", "宠物 QQ（SnowLuma 登录号）"),
                                          ("ws_url", "WebSocket 地址"), ("access_token", "Access Token"))):
            tk.Label(body, text=label, bg=PAGE, fg=INK, font=(FONT, 10)).grid(row=row, column=0, sticky="w", pady=8)
            variable = self.variables[key] = tk.StringVar(self, value=getattr(self.settings, key))
            entry = tk.Entry(body, textvariable=variable, font=(FONT, 10), bg=WHITE, fg=INK,
                             relief="flat", highlightthickness=1, highlightbackground=LINE,
                             highlightcolor=BLUE, show="•" if key == "access_token" else "")
            entry.grid(row=row, column=1, sticky="ew", padx=(14, 0), pady=8, ipady=7)
        body.columnconfigure(1, weight=1)
        tk.Button(body, text="读取 SnowLuma 配置…", command=self._import, bg=WHITE,
                  fg=BLUE, relief="flat", font=(FONT, 10), cursor="hand2", padx=12, pady=7).grid(
                      row=4, column=1, sticky="w", padx=(14, 0), pady=(4, 10))
        tk.Label(body, text="先在 SnowLuma 登录宠物 QQ，再填写你的用户 QQ。\n仅同步该用户的私聊；普通编号表情、语音、群聊暂不接入。\n图片形式的表情可接收；本地回复、绘画及主动聊天也会发到 QQ。",
                 bg=PAGE, fg=MUTED, font=(FONT, 9), justify="left", anchor="w").grid(
                     row=5, column=0, columnspan=2, sticky="ew", pady=(2, 12))
        self.status = tk.StringVar(self, value=status)
        tk.Label(body, textvariable=self.status, bg=PAGE, fg=BLUE, font=(FONT, 9),
                 wraplength=590, justify="left").grid(row=6, column=0, columnspan=2, sticky="w")
        footer = tk.Frame(self, bg=WHITE, padx=20, pady=14)
        footer.pack(fill="x", side="bottom", before=body)
        self.disable_button = tk.Button(footer, text="退出 QQ 模式", command=self._disable, bg=WHITE,
                                        fg=MUTED, relief="flat", padx=14, pady=9, font=(FONT, 10),
                                        state="normal" if enabled else "disabled", cursor="hand2")
        self.disable_button.pack(side="left")
        tk.Button(footer, text="保存并启用 QQ 模式", command=self._enable, bg=BLUE, fg=WHITE,
                  activebackground="#3f87c0", activeforeground=WHITE, relief="flat",
                  padx=18, pady=9, font=(FONT, 10), cursor="hand2").pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _import(self):
        saved = Path(self.settings.snowluma_config).parent if self.settings.snowluma_config else None
        initial = saved if saved is not None and saved.is_dir() else self.snow_folder / "config"
        filename = filedialog.askopenfilename(parent=self, title="选择 SnowLuma 的 onebot_账号.json",
                                              initialdir=str(initial if initial.exists() else self.snow_folder),
                                              filetypes=[("OneBot 配置", "onebot*.json"), ("JSON", "*.json")])
        if not filename:
            return
        try:
            imported = import_snowluma(Path(filename))
            self.settings.snowluma_config = imported.snowluma_config
            for key in ("expected_self_qq", "ws_url", "access_token"):
                self.variables[key].set(getattr(imported, key))
            self._import_pending = True
            self.status.set("新配置已读取，尚未用于连接。请确认用户 QQ，点击“保存并启用 QQ 模式”后生效。")
        except QQError as exc:
            messagebox.showerror("读取失败", str(exc), parent=self)

    def _enable(self):
        try:
            settings = QQSettings(**{key: variable.get().strip() for key, variable in self.variables.items()},
                                  snowluma_config=self.settings.snowluma_config).validate()
            pending = self._import_pending
            self._import_pending = False
            try:
                self.on_enable(settings)
            except Exception:
                self._import_pending = pending
                raise
            self.settings = settings
            self.disable_button.configure(state="normal")
        except (QQError, OSError) as exc:
            messagebox.showerror("无法启用 QQ 模式", str(exc), parent=self)

    def _disable(self):
        self.on_disable()
        self.disable_button.configure(state="disabled")

    def set_status(self, value):
        if not self._closed:
            if self._import_pending:
                self.status.set("新配置尚未启用，请点击“保存并启用 QQ 模式”。\n原连接状态：" + value)
            else:
                self.status.set(value)

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        super().destroy()
        self.variables.clear()
        self.status = None
        self.on_enable = self.on_disable = None
        for name, value in tuple(vars(self).items()):
            if isinstance(value, (tk.Misc, tk.Variable)):
                setattr(self, name, None)
