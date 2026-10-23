"""A compact Windows chat window with canvas bubbles and pixel scrolling."""

from __future__ import annotations

import os
import logging
import queue
import random
import re
import threading
import time
import tkinter as tk
import webbrowser
from collections import deque
from datetime import datetime
from tkinter import filedialog, messagebox
from urllib.parse import urlsplit

from PIL import Image, ImageGrab, ImageOps, ImageTk

from .chat_text import naturalize_text
from . import pet_core
from scripts.media_tools import media_path, split_media
from .system_tray import SystemTray
from .desktop_pet import DesktopPet, QUESTION_SECONDS, work_area
from scripts.image_inputs import MAX_IMAGES, attach_images, image_path as shared_image_path, save_image, split_images


from .chat_view import ConversationMixin, PAGE, CHAT_BG, WHITE, INK, MUTED, LINE, BLUE, BLUE_HOVER, BLUE_LIGHT, FONT, rounded
from .chat_history import HistoryArchive, LIVE_MESSAGE_LIMIT
from .history_window import HistoryWindow
from .qq_bridge import QQBridge, QQSettings
from .qq_settings import QQSettingsWindow
from scripts.screen_capture import ScreenCapturePrompt, screen_request_handler

IDLE_STATUS = "在这里，陪你聊聊"
MIN_REPLY_GAP = 1.0
MAX_REPLY_GAP = 3.0
IDLE_MIN_SECONDS, IDLE_MAX_SECONDS = 5 * 60, 30 * 60
SCREEN_REQUEST_TEXT = "我可以看看你的屏幕吗？"
IDLE_DRAW_TEXT = "我画了一幅画"
IDLE_REMINDER_TEXTS = (
    "你是不是把我忘了？(¬‿¬)",
    "悄悄冒个头，我还在这里哦。",
    "忙完了记得来找我聊两句呀。",
    "路过你的桌面，偷偷看你一眼。",
    "已经很久没听见你的声音啦。",
    "我在乖乖陪你，偶尔也看看我嘛。",
    "忙了这么久，要不要稍微休息一下？",
    "伸个懒腰，等你回来聊天。",
    SCREEN_REQUEST_TEXT,
    IDLE_DRAW_TEXT,
)


class PetWindow(ConversationMixin, tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        pet_core.ensure_files()
        self.pet_name = pet_core.pet_name()
        self.key_ready = pet_core.has_key()
        self.title(f"{self.pet_name} · 聊天宠物")
        self.geometry("920x740")
        self.minsize(640, 520)
        self.configure(bg=PAGE)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._pending_images: list[str] = []
        self._attachments_loading = False
        self._attachment_thumbnails = []
        self._tray = None
        self._desktop = None
        self._desktop_enabled = DesktopPet.mode_enabled(pet_core.KEY_FILE.parent / "desktop_pet.json")
        self._tray_start_job = None
        self._background_requested = self._background = self._closing = False
        self._unread_count = 0
        self._background_notified = False
        self._last_user_at = time.monotonic()
        self._next_idle_at = self._last_user_at + random.uniform(IDLE_MIN_SECONDS, IDLE_MAX_SECONDS)
        self._last_idle_reminder = ""
        self._active_screen_request = None
        self._pending_tool_screen_reason = None
        self._screen_tool_reason = None
        self._tool_screen_dialog = None
        self._tool_screen_timeout = None
        self._capture_restore_chat = False
        self._capture_reason = None
        self._screen_request_until = 0.0
        self._capturing_screen = False
        self._capture_job = None
        self._pending_replies: deque[tuple[str, str]] = deque()
        self._delivery_job = None
        self._next_reply_at = 0.0
        self._worker_done = False
        self._init_conversation()
        self.history_archive = HistoryArchive(pet_core.memory_catalog())
        self._history_window = None
        self._qq_bridge = self._qq_turn_bridge = None
        self._qq_window = None
        self._qq_status = "QQ 模式未启用"
        self._qq_pending: deque[tuple[str, int]] = deque()
        self.busy = self.planning = False
        self.next_plan_attempt = self.next_due_attempt = 0.0
        self._placeholder = False
        self._build()
        self._load_history()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_unmap)
        self.after(80, self._drain_events)
        self._animate_media()
        self.after(1500, self._schedule_tick)
        self.after(2000, self._refresh_identity)
        self.after(1000, self._desktop_tick)

    def destroy(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._clear_screen_prompt()
        self._pending_tool_screen_reason = None
        if self._qq_bridge is not None:
            self._qq_bridge.stop()
        if self._qq_window is not None and self._qq_window.winfo_exists():
            self._qq_window.destroy()
        if self._tray is not None:
            self._tray.stop()
        if self._desktop is not None:
            self._desktop.destroy()
        if self._history_window is not None and self._history_window.winfo_exists():
            self._history_window.destroy()
        self._stop_conversation()
        self._attachment_thumbnails.clear()
        for job in self.tk.call("after", "info"):
            self.after_cancel(job)
        super().destroy()
        for name, value in tuple(vars(self).items()):
            if isinstance(value, (tk.Misc, tk.Variable)):
                setattr(self, name, None)

    def _on_unmap(self, event) -> None:
        if event.widget is self and not self._closing and self.state() == "iconic":
            self.after_idle(self._minimize_to_tray)

    def _on_close(self) -> None:
        if not self._minimize_to_tray():
            self.destroy()

    def _minimize_to_tray(self) -> bool:
        if self._closing:
            return False
        self._background_requested = True
        if self._desktop_enabled and self._desktop is None:
            self._desktop = DesktopPet(self, (
                pet_core.ROOT / "Xig.png", pet_core.BUNDLED_ROOT / "Xig.png",
            ), pet_core.KEY_FILE.parent / "desktop_pet.json", self._restore_from_tray, self.destroy,
                animation_dirs=(pet_core.ROOT / "animation", pet_core.BUNDLED_ROOT / "animation"),
                on_qq_settings=self._open_qq_settings,
                on_close_pet=lambda: self._set_desktop_enabled(False))
            self._desktop.set_qq_enabled(self._qq_bridge is not None)
        self._background = True
        if self._history_window is not None and self._history_window.winfo_exists():
            self._history_window.withdraw()
            self._history_window.conversation._background = True
            self._history_window.conversation._release_animated_media()
        self._release_animated_media()
        self.withdraw()
        if self._desktop_enabled:
            self._desktop.show()
        if self._tray is None:
            try:
                self._tray = SystemTray(self.pet_name, (
                    pet_core.ROOT / "Xig.png", pet_core.BUNDLED_ROOT / "Xig.png",
                ), self.events)
                self._tray.set_pet_enabled(self._desktop_enabled)
                self._tray.start()
                self._tray_start_job = self.after(5000, self._tray_start_timeout)
            except Exception as exc:
                self._tray_failed(str(exc))
        return True

    def _set_desktop_enabled(self, enabled: bool) -> bool:
        try:
            DesktopPet.save_mode_enabled(pet_core.KEY_FILE.parent / "desktop_pet.json", enabled)
        except OSError as exc:
            messagebox.showerror("桌宠设置保存失败", str(exc), parent=self)
            return False
        self._desktop_enabled = bool(enabled)
        self.pet_mode_button.configure(text="桌宠：开" if enabled else "桌宠：关")
        if self._tray is not None:
            self._tray.set_pet_enabled(enabled)
        if enabled:
            self._schedule_next_idle()
            if self._background:
                self._minimize_to_tray()
        else:
            self._clear_screen_prompt()
            if self._desktop is not None:
                self._desktop.hide()
                self._desktop.destroy()
                self._desktop = None
            if self._background and (self._tray is None or not self._tray.ready.is_set()):
                self._restore_from_tray()
        return True

    def _tray_start_timeout(self) -> None:
        self._tray_start_job = None
        if self._tray is not None and not self._tray.ready.is_set():
            self._tray_failed("系统托盘未能启动。")

    def _tray_failed(self, detail: str) -> None:
        if self._tray_start_job is not None:
            self.after_cancel(self._tray_start_job)
            self._tray_start_job = None
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        # The desktop pet remains an entry point even if the optional tray fails.
        logging.warning("System tray unavailable: %s", detail)
        if self._background and not self._desktop_enabled:
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self._clear_screen_prompt()
        self._background_requested = self._background = False
        self._unread_count = 0
        if self._desktop is not None:
            self._desktop.hide()
        if self._tray is not None:
            self._tray.set_title(self.pet_name)
            self._tray.clear_notification()
        self.deiconify()
        if self._layout_dirty:
            self._request_layout()
        self._request_bottom()
        self.lift()
        self.input.focus_set()

    def _notify_background_message(self, content: str) -> None:
        self._clear_screen_prompt()
        text, media = split_media(content)
        preview = naturalize_text(text).strip() or ("发来了一张图片" if media else "有新消息")
        if self._desktop_enabled and self._desktop is not None:
            self._desktop.show_bubble(preview)
        self._unread_count += 1
        if self._tray is None:
            return
        self._tray.set_title(f"{self.pet_name} · {self._unread_count} 条新消息")
        if not self._background_notified:
            self._tray.notify(self.pet_name, preview)
            self._background_notified = True

    def _desktop_tick(self) -> None:
        if (not self._screen_tool_reason and self._desktop_enabled and self._background and self._desktop is not None and not self.busy
                and not self.planning and not self._qq_pending and not self._attachments_loading and not self._desktop.has_bubble
                and time.monotonic() >= self._next_idle_at):
            choices = [text for text in IDLE_REMINDER_TEXTS if text != self._last_idle_reminder]
            if self._qq_bridge is not None:
                choices = [text for text in choices if text != SCREEN_REQUEST_TEXT]
            self._last_idle_reminder = random.choice(choices)
            if self._last_idle_reminder == SCREEN_REQUEST_TEXT:
                token = self._active_screen_request = object()
                self._screen_request_until = time.monotonic() + QUESTION_SECONDS
                self._desktop.show_question(SCREEN_REQUEST_TEXT,
                    lambda accepted: self._on_screen_answer(token, accepted))
            elif self._last_idle_reminder == IDLE_DRAW_TEXT:
                if pet_core.has_key():
                    self._desktop.show_bubble("我想画一幅画送给你，等我一下。", preview=False)
                    self._begin_turn()
                    self.status.set("正在画画…")
                    threading.Thread(target=self._idle_drawing_worker, daemon=True).start()
                else:
                    self._desktop.show_bubble("想画点东西给你看，先帮我设置一下 Key 吧。", preview=False)
            else:
                self._desktop.show_bubble(self._last_idle_reminder, preview=False)
            self._schedule_next_idle()
        self.after(1000, self._desktop_tick)

    def _idle_drawing_worker(self) -> None:
        try:
            result = pet_core.create_idle_drawing()
            self.events.put(("idle_drawing_done", result))
        except Exception as exc:
            self.events.put(("idle_drawing_error", str(exc)))

    def _schedule_next_idle(self) -> None:
        self._next_idle_at = time.monotonic() + random.uniform(IDLE_MIN_SECONDS, IDLE_MAX_SECONDS)

    def _reset_idle_timer(self) -> None:
        self._last_user_at = time.monotonic()
        self._schedule_next_idle()

    def _queue_screen_request(self, reason) -> None:
        if not self._closing:
            self.events.put(("screen_capture_requested", reason))

    def _clear_screen_prompt(self) -> None:
        self._active_screen_request = None
        self._screen_tool_reason = None
        if self._tool_screen_timeout is not None:
            self.after_cancel(self._tool_screen_timeout)
            self._tool_screen_timeout = None
        if self._tool_screen_dialog is not None:
            if self._tool_screen_dialog.winfo_exists():
                self._tool_screen_dialog.destroy()
            self._tool_screen_dialog = None

    def _show_tool_screen_request(self) -> None:
        if self._closing or self.busy or not self._pending_tool_screen_reason:
            return
        reason = self._pending_tool_screen_reason
        self._pending_tool_screen_reason = None
        self._clear_screen_prompt()
        self._screen_tool_reason = reason
        token = self._active_screen_request = object()
        self._screen_request_until = time.monotonic() + QUESTION_SECONDS
        answer = lambda accepted: self._on_screen_answer(token, accepted)
        if self._background and self._desktop_enabled and self._desktop is not None:
            self._desktop.show_question(SCREEN_REQUEST_TEXT + "\n" + reason[:50], answer)
        else:
            self._tool_screen_dialog = ScreenCapturePrompt(self, reason, answer, QUESTION_SECONDS)
        self._tool_screen_timeout = self.after(QUESTION_SECONDS * 1000, lambda: answer(False))

    def _on_screen_answer(self, token, accepted) -> None:
        if self._closing or token is not self._active_screen_request:
            return
        reason = self._screen_tool_reason
        self._clear_screen_prompt()
        if time.monotonic() >= self._screen_request_until:
            self._maybe_start_qq_reply()
            return
        self._schedule_next_idle()
        if accepted is not True:
            self._maybe_start_qq_reply()
            return
        if self.busy or self._attachments_loading or not pet_core.has_key():
            if self._desktop is not None and self._background:
                self._desktop.show_bubble("现在还不能发送截图，稍后再问你。", preview=False)
            return
        self._reset_idle_timer()
        self._capturing_screen = True
        self._begin_turn()
        self.status.set("正在截取本次同意分享的屏幕…")
        pet = self._desktop
        target = pet if self._background and pet is not None else self
        bbox = work_area(target, target.winfo_rootx() + target.winfo_width() / 2,
                         target.winfo_rooty() + target.winfo_height() / 2, full_monitor=True)
        self._capture_reason = reason
        self._capture_restore_chat = bool(self.winfo_viewable())
        if pet is not None:
            pet.hide()
        if self._capture_restore_chat:
            self.withdraw()
        # Let the desktop compositor remove our pet and consent bubble first.
        self._capture_job = self.after(180, lambda: self._launch_screen_capture(bbox))

    def _launch_screen_capture(self, bbox) -> None:
        self._capture_job = None
        if not self._closing and self._capturing_screen:
            threading.Thread(target=self._screen_capture_worker, args=(bbox,), daemon=True).start()

    def _screen_capture_worker(self, bbox) -> None:
        if self._closing or not self._capturing_screen:
            return
        try:
            image = ImageGrab.grab(bbox=bbox, all_screens=True)
            try:
                name = save_image(image, pet_core.HISTORY.parent / "images")
            finally:
                image.close()
            if not self._closing:
                self.events.put(("screen_captured", name))
        except Exception as exc:
            if not self._closing:
                self.events.put(("screen_capture_error", str(exc)))

    def _screen_capture_complete(self, name) -> None:
        if not self._capturing_screen:
            return
        self._capturing_screen = False
        self._restore_after_screen_capture()
        text = ("我同意分享这一次截图。请根据屏幕继续处理刚才的请求：" + self._capture_reason
                if self._capture_reason else "你刚才问能否查看屏幕，我同意分享这一次截图。看看我现在在做什么，按你的性格自然聊两句吧。")
        self._capture_reason = None
        content = attach_images(text, [name])
        self._add_message("user", content)
        threading.Thread(target=self._worker, args=(content,), daemon=True).start()

    def _restore_after_screen_capture(self) -> None:
        if self._background and self._desktop_enabled and self._desktop is not None:
            self._desktop.show()
        if self._capture_restore_chat:
            self._capture_restore_chat = False
            self.deiconify()
            self.lift()

    def _build(self) -> None:
        header = tk.Frame(self, bg=WHITE, height=82)
        header.pack(fill="x")
        header.pack_propagate(False)
        self._avatar_label(header, 52, WHITE).pack(side="left", padx=(26, 12), pady=14)
        heading = tk.Frame(header, bg=WHITE)
        heading.pack(side="left")
        self.name_var = tk.StringVar(value=self.pet_name)
        tk.Label(heading, textvariable=self.name_var, bg=WHITE, fg=INK, font=(FONT, 16, "bold"), anchor="w").pack(anchor="w")
        self.status = tk.StringVar(value=IDLE_STATUS)
        tk.Label(heading, textvariable=self.status, bg=WHITE, fg=MUTED, font=(FONT, 9), anchor="w").pack(anchor="w", pady=(3, 0))
        self.key_var = tk.StringVar(value=self._key_status_text())
        self.key_button = tk.Button(header, text="API Key 设置", command=lambda: self._open_file(pet_core.KEY_FILE), bg=WHITE, fg="#7396af", activebackground=PAGE, activeforeground=BLUE, relief="flat", bd=0, font=(FONT, 9), cursor="hand2", padx=14, pady=9)
        self.key_button.pack(side="right", padx=22)
        self.history_button = tk.Button(header, text="聊天记录", command=self._open_history,
                                        bg=WHITE, fg=BLUE, activebackground=PAGE, relief="flat",
                                        bd=0, font=(FONT, 10), cursor="hand2", padx=12, pady=9)
        self.history_button.pack(side="right")
        self.pet_mode_button = tk.Button(header, text="桌宠：开" if self._desktop_enabled else "桌宠：关",
                                         command=lambda: self._set_desktop_enabled(not self._desktop_enabled),
                                         bg=WHITE, fg=BLUE, activebackground=PAGE, relief="flat",
                                         bd=0, font=(FONT, 9), cursor="hand2", padx=10, pady=9)
        self.pet_mode_button.pack(side="right")
        tk.Frame(self, bg=LINE, height=1).pack(fill="x")
        composer_area = tk.Frame(self, bg=PAGE, padx=24, pady=14)
        composer_area.pack(side="bottom", fill="x")
        self.attachment_frame = tk.Frame(composer_area, bg=PAGE)
        self.composer = tk.Canvas(composer_area, height=108, bg=PAGE, bd=0, highlightthickness=0)
        self.composer.pack(fill="x")
        self.input = tk.Text(self.composer, height=2, wrap="word", bg=WHITE, fg=INK, relief="flat", font=(FONT, 11), insertbackground=BLUE, bd=0, highlightthickness=0, padx=0, pady=0)
        self.input_window = self.composer.create_window(18, 16, window=self.input, anchor="nw", height=48)
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<FocusIn>", self._remove_placeholder)
        self.input.bind("<FocusOut>", self._show_placeholder)
        self.input.bind("<<Paste>>", self._paste_image)
        self.image_button = tk.Button(self.composer, text="＋ 图片", command=self._choose_images,
                                      bg=WHITE, fg=BLUE, activebackground=PAGE, relief="flat", bd=0,
                                      cursor="hand2", font=(FONT, 9))
        self.composer.create_window(14, 66, window=self.image_button, anchor="nw", width=76, height=32)
        self.composer.create_text(102, 85, text="Enter 发送 · Shift+Enter 换行 · Ctrl+V 粘贴图片", anchor="w", fill="#a0b3c1", font=(FONT, 8))
        self.send_button = tk.Canvas(self.composer, width=78, height=36, bg=WHITE, highlightthickness=0, cursor="hand2")
        self._send_shape = rounded(self.send_button, 0, 0, 78, 36, 12, fill=BLUE, outline="", disabledfill="#bed8eb")
        self.send_button.create_text(39, 18, text="发送 ↑", fill=WHITE, disabledfill=WHITE, font=(FONT, 10, "bold"))
        self.send_button.bind("<Button-1>", lambda _event: self._send())
        self.send_button.bind("<Enter>", lambda _event: self.send_button.itemconfigure(self._send_shape, fill=BLUE_HOVER))
        self.send_button.bind("<Leave>", lambda _event: self.send_button.itemconfigure(self._send_shape, fill=BLUE))
        self.send_window = self.composer.create_window(0, 65, window=self.send_button, anchor="nw")
        self.composer.bind("<Configure>", self._resize_composer)
        self._show_placeholder()
        self._build_conversation(self)

    def _resize_composer(self, event) -> None:
        self.composer.delete("composer_background")
        shape = rounded(self.composer, 0, 0, event.width, 107, 18, fill=WHITE, outline=LINE, tags="composer_background")
        self.composer.tag_lower(shape)
        self.composer.itemconfigure(self.input_window, width=max(180, event.width - 36))
        self.composer.coords(self.send_window, event.width - 94, 65)

    def _choose_images(self) -> None:
        if self.busy or self._attachments_loading:
            return
        paths = filedialog.askopenfilenames(parent=self, title="选择要发送的图片", filetypes=[
            ("图片", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"), ("所有文件", "*.*"),
        ])
        if paths:
            self._queue_images(list(paths))

    def _paste_image(self, _event=None):
        try:
            value = ImageGrab.grabclipboard()
        except (OSError, ValueError, NotImplementedError):
            return None
        if isinstance(value, Image.Image):
            self._queue_images([value])
            return "break"
        if isinstance(value, list):
            sources = [path for path in value if str(path).lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))]
            if sources:
                self._queue_images(sources)
                return "break"
        return None  # Preserve Tk's normal text paste behavior.

    def _queue_images(self, sources) -> None:
        if self.busy or self._attachments_loading:
            return
        available = MAX_IMAGES - len(self._pending_images)
        if len(sources) > available:
            messagebox.showinfo("图片数量", f"每条消息最多 {MAX_IMAGES} 张图片，目前还可添加 {available} 张。", parent=self)
            return
        self._attachments_loading = True
        self.status.set("正在处理图片…")
        self._refresh_send_state()
        self._render_attachments()
        threading.Thread(target=self._image_worker, args=(sources,), daemon=True).start()

    def _image_worker(self, sources) -> None:
        saved, errors = [], []
        for source in sources:
            try:
                name = save_image(source, pet_core.HISTORY.parent / "images")
                if name not in saved:
                    saved.append(name)
            except Exception as exc:
                errors.append(str(exc))
        self.events.put(("attachments_ready", (saved, errors)))

    def _attachments_complete(self, saved, errors) -> None:
        self._attachments_loading = False
        for name in saved:
            if name not in self._pending_images:
                self._pending_images.append(name)
        self._render_attachments()
        self._refresh_send_state()
        if not self.busy:
            self.status.set(IDLE_STATUS)
        if errors:
            messagebox.showwarning("部分图片未添加", "\n".join(errors[:4]), parent=self)

    def _remove_image(self, name) -> None:
        if not self._attachments_loading and name in self._pending_images:
            self._pending_images.remove(name)
            self._render_attachments()

    def _render_attachments(self) -> None:
        for child in self.attachment_frame.winfo_children():
            child.destroy()
        self._attachment_thumbnails.clear()
        if not self._pending_images and not self._attachments_loading:
            self.attachment_frame.pack_forget()
            return
        self.attachment_frame.pack(fill="x", pady=(0, 8), before=self.composer)
        for name in self._pending_images:
            chip = tk.Frame(self.attachment_frame, bg=WHITE, padx=6, pady=5)
            chip.pack(side="left", padx=(0, 8))
            try:
                with Image.open(shared_image_path(pet_core.HISTORY.parent / "images", name)) as source:
                    preview = ImageOps.contain(source.convert("RGBA"), (68, 54), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(preview, master=self)
                self._attachment_thumbnails.append(photo)
                tk.Label(chip, image=photo, bg=WHITE).pack(side="left")
            except (OSError, ValueError):
                tk.Label(chip, text="图片已缺失", bg=WHITE, fg=MUTED).pack(side="left")
            tk.Button(chip, text="×", command=lambda item=name: self._remove_image(item),
                      state="disabled" if self._attachments_loading else "normal",
                      bg=WHITE, fg=MUTED, relief="flat", bd=0, cursor="hand2").pack(side="right", anchor="n")
        if self._attachments_loading:
            tk.Label(self.attachment_frame, text="正在添加图片…", bg=PAGE, fg=MUTED, font=(FONT, 9)).pack(side="left")

    def _refresh_send_state(self) -> None:
        state = "disabled" if self.busy or self._attachments_loading else "normal"
        self.send_button.configure(state=state)
        self.image_button.configure(state=state)

    def _show_placeholder(self, _event=None) -> None:
        if not self.input.get("1.0", "end-1c").strip():
            self._placeholder = True
            self.input.configure(fg="#a0b3c1")
            self.input.insert("1.0", f"和{self.pet_name}说点什么…")

    def _remove_placeholder(self, _event=None) -> None:
        if self._placeholder:
            self.input.delete("1.0", "end")
            self.input.configure(fg=INK)
            self._placeholder = False

    def _add_message(self, role: str, content: str) -> None:
        had_welcome = any(item["role"] == "welcome" for item in self.messages)
        if role in {"user", "assistant"} and had_welcome:
            self.messages = [item for item in self.messages if item["role"] != "welcome"]
        message = {"role": role, "content": content}
        self.messages.append(message)
        trimmed = len(self.messages) > LIVE_MESSAGE_LIMIT
        if trimmed:
            del self.messages[:-LIVE_MESSAGE_LIMIT]
        if self._background:
            self._layout_dirty = True
            self._stick_to_bottom = True
            return
        if trimmed or had_welcome or self._layout_width <= 1 or self._layout_job is not None:
            self._request_layout()
        else:
            self._draw_message(message, len(self.messages) - 1)
            self._update_scroll_region()
        self._request_bottom()

    def _redraw_messages(self) -> None:
        self._request_layout()

    def _load_history(self) -> None:
        records = self.history_archive.recent()
        if not records:
            self._add_message("welcome", "")
        else:
            for item in records:
                self._add_message(item["role"], item["content"])

    def _open_history(self) -> None:
        if self._history_window is None or not self._history_window.winfo_exists():
            self._history_window = HistoryWindow(self, pet_core.memory_catalog(), self.pet_name)
        else:
            self._history_window.deiconify()
            self._history_window.conversation._background = self._history_window.day is None
            self._history_window.refresh()
        self._history_window.lift()

    def _open_qq_settings(self) -> None:
        if self._qq_window is None or not self._qq_window.winfo_exists():
            self._qq_window = QQSettingsWindow(self, pet_core.KEY_FILE.parent / "qq_bridge.json",
                pet_core.ROOT / "tem" / "SnowLuma-v2.1.1-win-x64-lite", self._enable_qq, self._disable_qq,
                self._qq_status, self._qq_bridge is not None)
        self._qq_window.deiconify()
        self._qq_window.lift()

    def _set_qq_status(self, status) -> None:
        self._qq_status = status
        if self._qq_window is not None and self._qq_window.winfo_exists():
            self._qq_window.set_status(status)

    def _enable_qq(self, settings: QQSettings) -> None:
        bridge = QQBridge(settings, self.events, pet_core.HISTORY.parent / "images", pet_core.MEDIA_DIR)
        settings.save(pet_core.KEY_FILE.parent / "qq_bridge.json")
        self._disable_qq()
        self._qq_bridge = bridge
        try:
            bridge.start()
        except Exception:
            bridge.stop()
            self._qq_bridge = None
            raise
        self._set_qq_status("QQ 模式已开启，正在连接 SnowLuma…")
        if self._desktop is not None:
            self._desktop.set_qq_enabled(True)

    def _disable_qq(self) -> None:
        if self._qq_bridge is not None:
            self._qq_bridge.stop()
        self._qq_bridge = None
        pending = len(self._qq_pending)
        self._qq_pending.clear()
        self._set_qq_status("QQ 模式未启用" + (f"；{pending} 条待回复消息已留档，未继续自动回复。" if pending else ""))
        if self._desktop is not None:
            self._desktop.set_qq_enabled(False)

    def _receive_qq_message(self, content: str) -> None:
        content = content.strip()
        if not content:
            return
        try:
            line = pet_core.append_history("user", content)
        except Exception:
            self._set_qq_status("QQ 消息保存失败，请检查 memory 文件夹；未调用模型。")
            return
        self._add_message("user", content)
        self._reset_idle_timer()
        self._qq_pending.append((content, line))
        self._maybe_start_qq_reply()

    def _maybe_start_qq_reply(self) -> None:
        if (self._closing or self._qq_bridge is None or not self._qq_pending or self.busy or self.planning
                or self._attachments_loading or self._capturing_screen or self._screen_tool_reason):
            return
        if not pet_core.has_key():
            self._set_qq_status("QQ 消息已留档，等待填写 DeepSeek API Key 后回复。")
            return
        content, line = self._qq_pending.popleft()
        self._begin_turn()
        threading.Thread(target=self._qq_reply_worker, args=(self.events, content, line, self._queue_screen_request), daemon=True).start()

    @staticmethod
    def _qq_reply_worker(events, content, line, on_screen_request=None):
        try:
            with screen_request_handler(on_screen_request):
                pet_core.reply(content, recorded_line=line,
                               on_reply=lambda answer: events.put(("reply", answer)))
            events.put(("turn_done", ""))
        except Exception as exc:
            events.put(("error", str(exc)))

    def _key_status_text(self) -> str:
        return "API Key 已设置" if self.key_ready else "API Key 未设置"

    def _welcome_text(self) -> str:
        return "今天有什么想分享的吗？" if self.key_ready else "点右上角设置 API Key，就可以开始聊天了。"

    def _refresh_identity(self) -> None:
        name, key_ready = pet_core.pet_name(), pet_core.has_key()
        changed = name != self.pet_name or key_ready != self.key_ready
        if name != self.pet_name:
            self.pet_name = name
            self.name_var.set(name)
            self.title(f"{name} · 聊天宠物")
            if self._tray is not None:
                self._tray.set_title(f"{name} · {self._unread_count} 条新消息" if self._unread_count else name)
            if self._placeholder:
                self.input.delete("1.0", "end")
                self._placeholder = False
                self._show_placeholder()
        if key_ready != self.key_ready:
            self.key_ready = key_ready
            self.key_var.set(self._key_status_text())
            if key_ready:
                self.next_plan_attempt = 0.0
        if changed:
            self._redraw_messages()
        self.after(2000, self._refresh_identity)

    def _open_file(self, path) -> None:
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror("打开文件失败", str(exc), parent=self)

    def _schedule_tick(self) -> None:
        now = datetime.now().astimezone()
        plan = pet_core.daily_plan()
        has_today = plan.has_day(now.date().isoformat())
        if not plan.has_generated(now.date().isoformat()) and not self.planning and time.monotonic() >= self.next_plan_attempt:
            self.planning = True
            threading.Thread(target=self._plan_worker, daemon=True).start()
        if has_today and not self.busy and not self.planning and not self._capturing_screen and not self._screen_tool_reason and time.monotonic() >= self.next_due_attempt:
            event = plan.due(now)
            if event:
                self._begin_turn()
                threading.Thread(target=self._proactive_worker, args=(now.date().isoformat(), event), daemon=True).start()
        self.after(15000, self._schedule_tick)

    def _plan_worker(self) -> None:
        try:
            pet_core.plan_today()
            self.events.put(("plan_done", ""))
        except Exception as exc:
            self.events.put(("plan_error", str(exc)))

    def _proactive_worker(self, day: str, event: dict) -> None:
        try:
            first = True
            def deliver(answer: str) -> None:
                nonlocal first
                self.events.put(("proactive_reply" if first else "reply", answer))
                first = False
            with screen_request_handler(self._queue_screen_request):
                pet_core.proactive_reply(event["topic"], on_reply=deliver)
            pet_core.daily_plan().mark_sent(day, event["time"], event["topic"], sent_at=datetime.now().astimezone())
            self.events.put(("turn_done", ""))
        except Exception as exc:
            if not first:
                # Do not repeat a contact if a later bubble failed after the first was saved.
                try:
                    pet_core.daily_plan().mark_sent(day, event["time"], event["topic"], sent_at=datetime.now().astimezone())
                except Exception:
                    logging.exception("Could not persist proactive delivery status")
            self.events.put(("proactive_error", str(exc)))

    def _on_enter(self, event) -> str | None:
        if event.state & 0x1:
            return None
        self._send()
        return "break"

    def _send(self) -> None:
        if self.busy or self._attachments_loading:
            return
        text = "" if self._placeholder else self.input.get("1.0", "end-1c").strip()
        if not text and not self._pending_images:
            return
        if not pet_core.has_key():
            messagebox.showwarning("请先设置 API Key", "请先通过右上角设置填写 DeepSeek API Key。", parent=self)
            return
        try:
            for name in self._pending_images:
                shared_image_path(pet_core.HISTORY.parent / "images", name)
        except (OSError, ValueError) as exc:
            messagebox.showwarning("图片不可用", str(exc), parent=self)
            return
        content = attach_images(text, self._pending_images)
        self._clear_screen_prompt()
        self._pending_tool_screen_reason = None
        self._reset_idle_timer()
        self.input.delete("1.0", "end")
        self._placeholder = False
        self.input.configure(fg=INK)
        self._pending_images.clear()
        self._render_attachments()
        self._add_message("user", content)
        self._begin_turn()
        threading.Thread(target=self._worker, args=(content,), daemon=True).start()

    def _begin_turn(self) -> None:
        self.busy = True
        self._qq_turn_bridge = self._qq_bridge
        self._background_notified = False
        self._worker_done = False
        self._next_reply_at = 0.0
        self._refresh_send_state()
        self.status.set(f"{self.pet_name}正在回复…")

    def _worker(self, content: str) -> None:
        try:
            with screen_request_handler(self._queue_screen_request):
                pet_core.reply(content, on_reply=lambda answer: self.events.put(("reply", answer)))
            self.events.put(("turn_done", ""))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _finish_turn(self) -> None:
        self.busy = False
        self._refresh_send_state()
        self.status.set("正在处理图片…" if self._attachments_loading else IDLE_STATUS)
        if not self._background:
            self.input.focus_set()
        self._show_tool_screen_request()
        self._maybe_start_qq_reply()

    def _schedule_delivery(self) -> None:
        if self._delivery_job is not None or not self._pending_replies:
            return
        remaining = self._next_reply_at - time.monotonic()
        if remaining <= 0:
            self._deliver_next_reply()
        else:
            self._delivery_job = self.after(max(1, int(remaining * 1000 + 0.999)), self._deliver_next_reply)

    def _deliver_next_reply(self) -> None:
        self._delivery_job = None
        if not self._pending_replies:
            return
        if time.monotonic() < self._next_reply_at:
            self._schedule_delivery()
            return
        kind, content = self._pending_replies.popleft()
        self._add_message("error" if kind in {"error", "proactive_error"} else "assistant", content)
        if (kind in {"reply", "proactive_reply"} and self._qq_bridge is not None
                and self._qq_bridge is self._qq_turn_bridge):
            self._qq_bridge.send(content)
        if self._background and kind in {"reply", "proactive_reply"}:
            self._notify_background_message(content)
        elif kind == "proactive_reply":
            self.bell()
            self.deiconify()
            self.lift()
        self._next_reply_at = time.monotonic() + random.uniform(MIN_REPLY_GAP, MAX_REPLY_GAP)
        if self._pending_replies:
            self._schedule_delivery()
        elif self._worker_done:
            self._finish_turn()

    def _drain_events(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind in {"qq_message", "qq_status", "qq_send_error"}:
                bridge, payload = value
                if bridge is not self._qq_bridge:
                    continue
                if kind == "qq_message":
                    self._receive_qq_message(payload)
                elif kind == "qq_status":
                    self._set_qq_status(payload)
                else:
                    self._set_qq_status(f"QQ 同步失败：{payload} 本地消息及记录仍然保留。")
                    self._add_message("error", f"QQ 同步失败：{payload}\n本地消息及记录仍然保留，可在 QQ 接入设置查看连接状态。")
                    if self._desktop_enabled and self._background and self._desktop is not None:
                        self._desktop.show_bubble("QQ 同步失败，请打开 QQ 接入设置查看。", preview=False)
            elif kind == "attachments_ready":
                self._attachments_complete(*value)
            elif kind == "screen_capture_requested":
                self._pending_tool_screen_reason = value
                self._show_tool_screen_request()
            elif kind == "screen_captured":
                self._screen_capture_complete(value)
            elif kind == "screen_capture_error":
                self._capturing_screen = False
                self._capture_reason = None
                self._restore_after_screen_capture()
                if self._background and self._desktop_enabled and self._desktop is not None:
                    self._desktop.show_bubble("这次截图失败了，请回聊天窗口查看。", preview=False)
                self._add_message("error", f"屏幕截图失败：{value}")
                self._finish_turn()
            elif kind in {"idle_drawing_done", "idle_drawing_error"}:
                if self._desktop_enabled and self._background and self._desktop is not None:
                    if kind == "idle_drawing_done":
                        caption, _pictures = split_media(value)
                        self._desktop.show_bubble(naturalize_text(caption).strip() or "我画了一幅画。")
                    else:
                        self._desktop.show_bubble("这次没画好，晚点再画给你看。", preview=False)
                self._worker_done = True
                self._finish_turn()
            elif kind == "tray_ready":
                if self._tray_start_job is not None:
                    self.after_cancel(self._tray_start_job)
                    self._tray_start_job = None
                if self._background_requested:
                    self._minimize_to_tray()
            elif kind == "tray_restore":
                self._restore_from_tray()
            elif kind == "tray_toggle_pet":
                enabling = not self._desktop_enabled
                if self._set_desktop_enabled(enabling) and enabling and not self._background:
                    self._minimize_to_tray()
            elif kind == "tray_qq_settings":
                self._open_qq_settings()
            elif kind == "tray_exit":
                self.destroy()
                return
            elif kind == "tray_error":
                self._tray_failed(value)
            elif kind == "plan_done":
                self.planning = False
            elif kind == "plan_error":
                self.planning = False
                self.next_plan_attempt = time.monotonic() + 300
            elif kind in {"reply", "proactive_reply"}:
                self._pending_replies.append((kind, value))
                self._schedule_delivery()
            elif kind == "turn_done":
                self._worker_done = True
                if not self._pending_replies and self._delivery_job is None:
                    self._finish_turn()
            else:
                if kind == "proactive_error":
                    self.next_due_attempt = time.monotonic() + 60
                self._worker_done = True
                self._pending_replies.append((kind, value))
                self._schedule_delivery()
        self._maybe_start_qq_reply()
        delay = 80 if self.busy or self._attachments_loading else (500 if self._background else 250)
        self.after(delay, self._drain_events)


def main() -> None:
    """Start the desktop chat application."""
    PetWindow().mainloop()


if __name__ == "__main__":
    main()
