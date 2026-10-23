"""Shared canvas conversation renderer for live chat and read-only history."""
from __future__ import annotations

import os
import re
import time
import tkinter as tk
import webbrowser
from collections import OrderedDict
from tkinter import messagebox
from urllib.parse import urlsplit
from PIL import Image, ImageOps, ImageTk

from . import pet_core
from .chat_text import naturalize_text
from scripts.media_tools import media_path, split_media
from scripts.image_inputs import image_path as shared_image_path, split_images

PAGE = "#f1f7fc"
CHAT_BG = PAGE
WHITE = "#ffffff"
INK = "#29485e"
MUTED = "#88a1b3"
LINE = "#e1ecf4"
BLUE = "#529bd3"
BLUE_HOVER = "#3f87c0"
BLUE_LIGHT = "#ddecfa"
FONT = "Microsoft YaHei UI"
URL_PATTERN = re.compile(r"https?://[^\s<>。！？]+")
MEDIA_CACHE_BYTES = 32 * 1024 * 1024
MEDIA_CACHE_ITEMS = 24
MAX_VISIBLE_ANIMATIONS = 3

def rounded(canvas, x1, y1, x2, y2, radius=16, **options):
    radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    return canvas.create_polygon(
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        smooth=True, splinesteps=24, **options,
    )


class ConversationMixin:
    def _init_conversation(self):
        self.messages = []
        self.avatar_images = {}
        self.media_images = OrderedDict()
        self._media_costs = {}
        self._gif_paths = set()
        self.animated_items = []
        self._display_images = []
        self._background = False
        self._layout_dirty = False
        self._layout_width = 0
        self._layout_y = 20
        self._layout_job = self._bottom_job = self._scroll_job = None
        self._media_job = None
        self._wheel_pending = 0.0
        self._stick_to_bottom = True
        self._show_timestamps = False

    def _build_conversation(self, parent):
        conversation = tk.Frame(parent, bg=CHAT_BG)
        conversation.pack(fill="both", expand=True)
        self._scroll_fraction = (0.0, 1.0)
        self._scroll_drag = None
        self.scrollbar = tk.Canvas(conversation, width=8, bg=CHAT_BG, highlightthickness=0)
        self.scrollbar.pack(side="right", fill="y", padx=(0, 5), pady=12)
        self.scrollbar.bind("<Configure>", lambda _event: self._paint_scrollbar())
        self.scrollbar.bind("<Button-1>", self._start_scroll_drag)
        self.scrollbar.bind("<B1-Motion>", self._drag_scrollbar)
        self.scrollbar.bind("<MouseWheel>", self._scroll_chat)
        self.chat_canvas = tk.Canvas(conversation, bg=CHAT_BG, highlightthickness=0, bd=0, yscrollincrement=1, yscrollcommand=self._sync_scrollbar)
        self.chat_canvas.pack(side="left", fill="both", expand=True)
        self.chat_canvas.bind("<Configure>", self._resize_chat)
        self.chat_canvas.bind("<MouseWheel>", self._scroll_chat)
        self.chat_canvas.bind("<Button-4>", self._scroll_chat)
        self.chat_canvas.bind("<Button-5>", self._scroll_chat)

    def _avatar_image(self, size: int) -> ImageTk.PhotoImage:
        if size not in self.avatar_images:
            path = pet_core.ROOT / "Xig.png"
            if not path.exists():
                path = pet_core.BUNDLED_ROOT / "Xig.png"
            try:
                with Image.open(path) as source:
                    image = ImageOps.contain(source.convert("RGBA"), (size, size), Image.Resampling.LANCZOS)
            except (OSError, ValueError):
                image = Image.new("RGBA", (size, size), BLUE_LIGHT)
            self.avatar_images[size] = ImageTk.PhotoImage(image, master=self)
        return self.avatar_images[size]

    def _avatar_label(self, parent, size, background) -> tk.Label:
        return tk.Label(parent, image=self._avatar_image(size), bg=background, bd=0)

    def _resize_chat(self, event) -> None:
        if event.width != self._layout_width:
            self._request_layout()
        else:
            self._update_scroll_region()
            if self._stick_to_bottom:
                self._request_bottom()

    def _request_layout(self) -> None:
        if self._background:
            self._layout_dirty = True
            return
        if self._layout_job is None:
            self._layout_job = self.after_idle(self._layout_messages)

    def _layout_messages(self) -> None:
        self._layout_job = None
        if self._background:
            self._layout_dirty = True
            return
        self._layout_dirty = False
        old_top = self.chat_canvas.canvasy(0)
        self.chat_canvas.delete("all")
        self.animated_items.clear()
        self._display_images.clear()
        self._layout_width = self.chat_canvas.winfo_width()
        self._layout_y = 20
        for index, message in enumerate(self.messages):
            self._draw_message(message, index)
        self._update_scroll_region()
        if self._stick_to_bottom:
            self._request_bottom()
        else:
            height = max(self._layout_y + 24, self.chat_canvas.winfo_height())
            self.chat_canvas.yview_moveto(old_top / max(1, height))
        self._wake_media()

    def _update_scroll_region(self) -> None:
        self.chat_canvas.configure(scrollregion=(0, 0, max(1, self._layout_width), max(self.chat_canvas.winfo_height(), self._layout_y + 24)))

    def _request_bottom(self) -> None:
        self._stick_to_bottom = True
        self._wheel_pending = 0.0
        if self._background:
            return
        if self._bottom_job is None:
            self._bottom_job = self.after_idle(self._apply_bottom)

    def _apply_bottom(self) -> None:
        self._bottom_job = None
        if not self._stick_to_bottom:
            return
        if self._layout_job is not None:
            self._bottom_job = self.after_idle(self._apply_bottom)
            return
        self._update_scroll_region()
        self.chat_canvas.yview_moveto(1.0)
        self._wake_media()

    def _scrollbar_scroll(self, *arguments) -> None:
        self._stick_to_bottom = False
        self._wheel_pending = 0.0
        self.chat_canvas.yview(*arguments)
        self._wake_media()

    def _sync_scrollbar(self, first, last) -> None:
        self._scroll_fraction = (float(first), float(last))
        self._paint_scrollbar()

    def _paint_scrollbar(self) -> None:
        self.scrollbar.delete("all")
        first, last = self._scroll_fraction
        if last - first >= 0.999:
            return
        height = self.scrollbar.winfo_height()
        thumb = max(28, (last - first) * height)
        top = first / max(0.001, 1 - (last - first)) * (height - thumb)
        rounded(self.scrollbar, 1, top, 6, top + thumb, 2, fill="#c5dce9", outline="")

    def _start_scroll_drag(self, event) -> None:
        first, last = self._scroll_fraction
        self._scroll_drag = (event.y, first)
        height = max(1, self.scrollbar.winfo_height())
        if not first * height <= event.y <= last * height:
            self._scrollbar_scroll("moveto", max(0, event.y / height - (last - first) / 2))
            self._scroll_drag = (event.y, self.chat_canvas.yview()[0])

    def _drag_scrollbar(self, event) -> None:
        if self._scroll_drag:
            origin, first = self._scroll_drag
            self._scrollbar_scroll("moveto", first + (event.y - origin) / max(1, self.scrollbar.winfo_height()))

    def _scroll_chat(self, event) -> str:
        self._stick_to_bottom = False
        delta = getattr(event, "delta", 0)
        if not delta:
            delta = 120 if getattr(event, "num", None) == 4 else -120
        self._wheel_pending += -delta / 120 * 64
        if self._scroll_job is None:
            self._scroll_job = self.after(8, self._animate_scroll)
        return "break"

    def _animate_scroll(self) -> None:
        self._scroll_job = None
        if abs(self._wheel_pending) < 0.5:
            self._wheel_pending = 0.0
            return
        step = (1 if self._wheel_pending > 0 else -1) * max(1, round(abs(self._wheel_pending) * 0.35))
        before = self.chat_canvas.yview()
        self.chat_canvas.yview_scroll(step, "units")
        self._wake_media()
        self._wheel_pending -= step
        if self.chat_canvas.yview() == before:
            self._wheel_pending = 0.0
        if abs(self._wheel_pending) >= 0.5:
            self._scroll_job = self.after(12, self._animate_scroll)

    def _load_media(self, filename: str, *, user_image=False, preview_only=False) -> tuple[list, list[int]] | None:
        try:
            path = shared_image_path(pet_core.HISTORY.parent / "images", filename) if user_image else media_path(pet_core.MEDIA_DIR, filename)
            key = (str(path), path.stat().st_mtime_ns, preview_only)
            if key not in self.media_images:
                frames, delays = [], []
                with Image.open(path) as source:
                    count = getattr(source, "n_frames", 1)
                    if count > 1:
                        self._gif_paths.add(str(path))
                    else:
                        self._gif_paths.discard(str(path))
                    for index in range(1 if preview_only else min(count, 80)):
                        source.seek(index)
                        frame = ImageOps.exif_transpose(source.convert("RGBA"))
                        frame.thumbnail((220, 200), Image.Resampling.LANCZOS)
                        frames.append(ImageTk.PhotoImage(frame, master=self))
                        delays.append(max(60, min(int(source.info.get("duration", 120)), 1000)))
                self.media_images[key] = (frames, delays)
                self._media_costs[key] = sum(frame.width() * frame.height() * 4 for frame in frames)
                while len(self.media_images) > 1 and (len(self.media_images) > MEDIA_CACHE_ITEMS
                        or sum(self._media_costs.values()) > MEDIA_CACHE_BYTES):
                    discarded, _ = self.media_images.popitem(last=False)
                    self._media_costs.pop(discarded, None)
            self.media_images.move_to_end(key)
            return self.media_images[key]
        except (OSError, ValueError, Image.DecompressionBombError):
            return None

    def _draw_message(self, message: dict[str, str], index: int) -> None:
        canvas = self.chat_canvas
        width = max(400, self._layout_width)
        column = min(850, width - 44)
        left = (width - column) / 2
        role = message["role"]
        if role == "welcome":
            canvas.create_image(width / 2, 56, image=self._avatar_image(88), anchor="n")
            canvas.create_text(width / 2, 165, text=f"和{self.pet_name}聊一会儿", fill=INK, font=(FONT, 16, "bold"))
            canvas.create_text(width / 2, 202, text=self._welcome_text(), fill=MUTED, font=(FONT, 10))
            self._layout_y = 245
            return
        outgoing = role == "user"
        content = message["content"]
        media, media_paths, links = [], [], []
        filenames = []
        if role == "user":
            content, filenames = split_images(content)
        if role == "assistant":
            content, filenames = split_media(content)
            content = naturalize_text(content)
            links = list(dict.fromkeys(url.rstrip(".,，；;！!？?)）") for url in URL_PATTERN.findall(content)))[:5]
            if links:
                content = URL_PATTERN.sub("", content)
                content = re.sub(r"(?m)^\s*(?:来源|参考|链接)[：:]?\s*$", "", content).strip()
        for filename in filenames:
            result = self._load_media(filename, user_image=role == "user", preview_only=True)
            if result:
                media.append(result)
                media_paths.append(shared_image_path(pet_core.HISTORY.parent / "images", filename) if role == "user" else media_path(pet_core.MEDIA_DIR, filename))
            else:
                content += "\n图片附件已缺失" if role == "user" else f"\n图片无法显示：{filename}"
        previous = self.messages[index - 1]["role"] if index else None
        grouped = previous == role and role in {"assistant", "user"}
        stamp = str(message.get("time", ""))[:16]
        previous_stamp = str(self.messages[index - 1].get("time", ""))[:16] if index else ""
        if self._show_timestamps and stamp and stamp != previous_stamp:
            canvas.create_text(width / 2, self._layout_y + 20, text=stamp.replace("T", " "),
                               fill=MUTED, font=(FONT, 8), tags="message_time")
            self._layout_y += 34
            grouped = False
        y = self._layout_y + (8 if grouped else 18)
        max_wrap = max(200, min(540, column - 110))
        text_item = None
        text_width = text_height = 0
        if content:
            text_item = canvas.create_text(0, 0, text=content, anchor="nw", width=max_wrap - 36, fill="#ac6863" if role == "error" else INK, font=(FONT, 11), tags="message_text")
            box = canvas.bbox(text_item)
            text_width, text_height = box[2] - box[0], box[3] - box[1]
        media_width = max((frames[0].width() for frames, _ in media), default=0)
        media_height = sum(frames[0].height() + 10 for frames, _ in media)
        bubble_width = max(72, text_width + 36, media_width + 24, 200 if links else 0)
        bubble_height = max(38, text_height + 26 + media_height + len(links) * 25)
        x = left + column - bubble_width if outgoing else left + 54
        sticker_only = role == "assistant" and bool(media) and not content and not links
        if sticker_only:
            bubble_height = media_height + 8
        fill = BLUE_LIGHT if outgoing else ("#fff1ef" if role == "error" else WHITE)
        if not sticker_only:
            shadow = rounded(canvas, x, y + 2, x + bubble_width, y + bubble_height + 2, 17, fill="#e6eff6", outline="")
            shape = rounded(canvas, x, y, x + bubble_width, y + bubble_height, 17, fill=fill, outline="")
            if text_item:
                canvas.tag_lower(shadow, text_item)
                canvas.tag_lower(shape, text_item)
        if not outgoing and role != "error" and not grouped:
            canvas.create_image(left, y - 1, image=self._avatar_image(42), anchor="nw", tags="pet_avatar")
        if text_item:
            canvas.coords(text_item, x + 18, y + 13)
        next_y = y + (text_height + 20 if content else 8)
        for (frames, delays), path in zip(media, media_paths):
            self._display_images.append(frames[0])
            item = canvas.create_image(x + 12, next_y, image=frames[0], anchor="nw", tags="chat_media")
            canvas.tag_bind(item, "<Button-1>", lambda _event, image=path: self._open_file(image))
            canvas.tag_bind(item, "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
            canvas.tag_bind(item, "<Leave>", lambda _event: canvas.configure(cursor=""))
            if str(path) in self._gif_paths:
                self.animated_items.append({"item": item, "preview": frames[0], "frames": frames,
                    "delays": delays, "path": path, "user_image": outgoing, "loaded": False,
                    "index": 0, "next": time.monotonic() + delays[0] / 1000})
            next_y += frames[0].height() + 10
        for number, url in enumerate(links):
            label = f"↗ {urlsplit(url).netloc or '来源'}"
            item = canvas.create_text(x + 18, next_y + number * 25 + 4, text=label, anchor="nw", fill=BLUE, font=(FONT, 9), tags="source_link")
            canvas.tag_bind(item, "<Button-1>", lambda _event, target=url: webbrowser.open_new_tab(target))
            canvas.tag_bind(item, "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
            canvas.tag_bind(item, "<Leave>", lambda _event: canvas.configure(cursor=""))
        self._layout_y = y + bubble_height

    def _wake_media(self):
        if self._media_job is not None:
            self.after_cancel(self._media_job)
            self._media_job = None
        self._animate_media()

    def _refresh_visible_media(self):
        now = time.monotonic()
        top = self.chat_canvas.canvasy(0)
        bottom = top + self.chat_canvas.winfo_height()
        active, wait = 0, 1000
        for animation in self.animated_items:
            box = self.chat_canvas.bbox(animation["item"])
            visible = box and box[3] >= top and box[1] <= bottom and active < MAX_VISIBLE_ANIMATIONS
            if not visible:
                if animation["loaded"]:
                    self.chat_canvas.itemconfigure(animation["item"], image=animation["preview"])
                    animation["frames"] = [animation["preview"]]
                    animation["loaded"] = False
                continue
            active += 1
            if not animation["loaded"]:
                result = self._load_media(animation["path"].name, user_image=animation["user_image"])
                if not result:
                    continue
                animation["frames"], animation["delays"] = result
                animation.update(loaded=True, index=0, next=now + result[1][0] / 1000)
            if now >= animation["next"]:
                animation["index"] = (animation["index"] + 1) % len(animation["frames"])
                self.chat_canvas.itemconfigure(animation["item"], image=animation["frames"][animation["index"]])
                animation["next"] = now + animation["delays"][animation["index"]] / 1000
            wait = min(wait, max(20, int((animation["next"] - now) * 1000)))
        return wait

    def _animate_media(self) -> None:
        if self._media_job is not None:
            self.after_cancel(self._media_job)
        self._media_job = None
        delay = 1000 if self._background or not self.winfo_ismapped() else self._refresh_visible_media()
        self._media_job = self.after(delay, self._animate_media)

    def _release_animated_media(self):
        for animation in self.animated_items:
            self.chat_canvas.itemconfigure(animation["item"], image=animation["preview"])
            animation["frames"] = [animation["preview"]]
            animation["loaded"] = False
        self.media_images.clear()
        self._media_costs.clear()
        self._gif_paths.clear()

    def clear_messages(self):
        self.messages = []
        self.chat_canvas.delete("all")
        self.animated_items.clear()
        self._display_images.clear()
        self.media_images.clear()
        self._media_costs.clear()
        self._gif_paths.clear()
        self._layout_y = 20

    def _open_file(self, path):
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror("打开文件失败", str(exc), parent=self)

    def _stop_conversation(self):
        for name in ("_layout_job", "_bottom_job", "_scroll_job", "_media_job"):
            job = getattr(self, name, None)
            if job is not None:
                self.after_cancel(job)
                setattr(self, name, None)
        self.animated_items.clear()
        self.media_images.clear()
        self._media_costs.clear()
        self._gif_paths.clear()
        self._display_images.clear()
        self.avatar_images.clear()
        self.messages.clear()


class ConversationFrame(ConversationMixin, tk.Frame):
    def __init__(self, master, pet_name):
        super().__init__(master, bg=CHAT_BG)
        self.pet_name = pet_name
        self._init_conversation()
        self._show_timestamps = True
        self._build_conversation(self)
        self._animate_media()

    def set_messages(self, records):
        self.clear_messages()
        self.messages = records
        self._wheel_pending = 0.0
        self._stick_to_bottom = False
        self.chat_canvas.yview_moveto(0)
        self._request_layout()

    def destroy(self):
        self._stop_conversation()
        super().destroy()
