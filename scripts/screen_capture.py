"""Explicit current user requests authorize one capture; model requests use UI consent."""

from contextlib import contextmanager
from contextvars import ContextVar
import re
import tkinter as tk

_request_handler = ContextVar("screen_request_handler", default=None)
_screenshot_supplied = ContextVar("screen_capture_supplied", default=False)


def direct_screen_request(content):
    """Recognize a current user's direct instruction, never tool/page/history text.

    This is used only on incoming chat/QQ messages, before model execution.
    Ambiguous requests retain the existing one-time confirmation UI.
    """
    if not isinstance(content, str):
        return False
    from .image_inputs import split_images
    text = split_images(content)[0]
    text = re.sub(r"```[\s\S]*?```|`[^`\n]*`|[“「‘\"'][^”」’\"'\n]*[”」’\"']", "", text)
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    clauses = [part.strip() for part in re.split(r"[，,。；;！!\n]+", text) if part.strip()]
    for clause in clauses:
        if re.search(r"(?:不要|别|不用|无需|不必|不想|不需要|禁止|停止|取消|不能|拒绝).{0,4}(?:截[屏图]|截.{0,3}[图屏]|看.{0,4}(?:屏幕|桌面))", clause):
            return False
    for clause in clauses:
        if re.search(r"怎么|如何|为什么|支持|功能|按钮|设置|权限|什么意思|代码|教程|必须|保存位置|保存在哪|在哪里|截图原理|截图方法", clause):
            continue
        if re.match(r"(?:如果|假如|假设|以后|下次|之前|上次|昨天|比如|例如|当你|当我|(?:他|她|别人)(?:说|让|叫|要求)|你说)", clause) or re.search(r"刚才说|之前说|以前说|上次说", clause):
            continue
        capture = re.search(r"截[屏图]|截.{0,3}[图屏]", clause)
        if capture:
            prefix = clause[:capture.start()]
            if re.search(r"告诉|解释|介绍|讲解|说明|描述|请问|查看|分析|解读|阅读|看一下|看看|假如|假设|如果|比如|例如", prefix):
                continue
            if re.search(r"之前|以前|昨天|上次|刚才", prefix) and not re.search(r"现在|这次|立即|请", prefix):
                continue
        if re.search(r"(?:请|帮我|给我|麻烦|现在|这次|直接|立即|马上).{0,10}(?:截[屏图]|截.{0,3}[图屏])", clause):
            return True
        if re.match(r"^(?:截[屏图]|截个[图屏]|截一张图|截一下图?)(?:$|给我|发给我|看看|看一下|吧|呀|啊|好吗|可以吗|吗|[？?])", clause):
            return True
        if re.search(r"(?:看看|看一下|看下|帮我看).{0,8}(?:屏幕|桌面)", clause):
            return True
    return False


@contextmanager
def screen_request_handler(handler, *, screenshot_supplied=False):
    requested = False
    def once(reason):
        nonlocal requested
        if not requested:
            handler(reason)
            requested = True
    token = _request_handler.set(once if handler is not None else None)
    supplied_token = _screenshot_supplied.set(screenshot_supplied is True)
    try:
        yield
    finally:
        _request_handler.reset(token)
        _screenshot_supplied.reset(supplied_token)


def request_screen_capture(reason):
    if not isinstance(reason, str) or not reason.strip():
        return {"error": "请简短说明为什么需要查看当前屏幕。"}
    if _screenshot_supplied.get():
        return {"status": "already_captured", "message": "本轮截图已经附在当前用户消息中。请直接分析已提供图片，不重复截图或要求点击确认。"}
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
