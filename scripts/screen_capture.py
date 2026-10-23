"""A model may request a screenshot; only the UI can grant one-time consent."""

from contextlib import contextmanager
from contextvars import ContextVar
import tkinter as tk

_request_handler = ContextVar("screen_request_handler", default=None)


@contextmanager
def screen_request_handler(handler):
    requested = False
    def once(reason):
        nonlocal requested
        if not requested:
            handler(reason)
            requested = True
    token = _request_handler.set(once if handler is not None else None)
    try:
        yield
    finally:
        _request_handler.reset(token)


def request_screen_capture(reason):
    if not isinstance(reason, str) or not reason.strip():
        return {"error": "请简短说明为什么需要查看当前屏幕。"}
    handler = _request_handler.get()
    if handler is None:
        return {"status": "unavailable", "message": "当前任务不支持请求截图。不能查看屏幕，也不要声称已经截图。"}
    handler(reason.strip()[:160])
    return {"status": "awaiting_user_confirmation", "message": "已请求本机用户确认。本次还没有截图，不能描述屏幕内容；确认并截图成功后，图片会作为新消息提供。不要重复请求或声称用户已经同意。"}


class ScreenCapturePrompt(tk.Toplevel):
    def __init__(self, master, reason, on_answer, seconds=45):
        from xigpet.chat_view import PAGE, WHITE, INK, MUTED, BLUE, FONT

        super().__init__(master)
        self.title("允许查看这一次屏幕吗？")
        self.geometry("480x330")
        self.resizable(False, False)
        self.configure(bg=PAGE)
        self.attributes("-topmost", True)
        self.on_answer = on_answer
        tk.Label(self, text="我可以看看你的屏幕吗？", bg=PAGE, fg=INK,
                 font=(FONT, 14, "bold")).pack(anchor="w", padx=24, pady=(22, 12))
        tk.Label(self, text=reason, bg=PAGE, fg=INK, font=(FONT, 10),
                 wraplength=430, justify="left").pack(anchor="w", padx=24)
        tk.Label(self, text="仅截取当前窗口所在显示器，发送给 DeepSeek 并保存到聊天记录。\n仅授权这一次；关闭或超时视为拒绝。", bg=PAGE, fg=MUTED,
                 font=(FONT, 9), justify="left", wraplength=430).pack(anchor="w", padx=24, pady=14)
        buttons = tk.Frame(self, bg=PAGE)
        buttons.pack(side="bottom", fill="x", padx=24, pady=18)
        tk.Button(buttons, text="允许这一次", command=lambda: self._answer(True),
                  bg=BLUE, fg=WHITE, relief="flat", padx=18, pady=8).pack(side="right")
        tk.Button(buttons, text="拒绝", command=lambda: self._answer(False),
                  bg=WHITE, fg=INK, relief="flat", padx=18, pady=8).pack(side="right", padx=10)
        self.protocol("WM_DELETE_WINDOW", lambda: self._answer(False))
        self._timeout_job = self.after(seconds * 1000, lambda: self._answer(False))

    def _answer(self, accepted):
        callback = self.on_answer
        self.destroy()
        if callback:
            callback(accepted)

    def destroy(self):
        if getattr(self, "_timeout_job", None) is not None:
            self.after_cancel(self._timeout_job)
            self._timeout_job = None
        self.on_answer = None
        super().destroy()
