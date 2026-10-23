"""A draggable, gently floating desktop companion and its speech bubble."""

from __future__ import annotations

import json
import logging
import os
import time
import tkinter as tk
from pathlib import Path
from tkinter import simpledialog

from PIL import Image, ImageOps, ImageTk
from .pet_behavior import FLOAT_RADIUS_X, FLOAT_RADIUS_Y, PetBehavior


TRANSPARENT = "#ff00ff"
DEFAULT_SIZE = 180
MIN_SIZE, MAX_SIZE = 64, 400
BUBBLE_SECONDS = 10
QUESTION_SECONDS = 45
FONT = "Microsoft YaHei UI"


def message_preview(text: str) -> str:
    text = " ".join(text.split())
    return text[:20] + ("…" if len(text) > 20 else "")


def position_window(widget, width, height, x, y):
    # Tk's negative geometry offsets are measured from the screen's far edge.
    x, y = round(x), round(y)
    gx = f"+{x}" if x >= 0 else f"-{widget.winfo_screenwidth() - width - x}"
    gy = f"+{y}" if y >= 0 else f"-{widget.winfo_screenheight() - height - y}"
    widget.geometry(f"{width}x{height}{gx}{gy}")


def work_area(widget, x: float, y: float, *, full_monitor=False) -> tuple[int, int, int, int]:
    """Return the nearest monitor's usable bounds, including negative origins."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        monitor = user32.MonitorFromPoint(wintypes.POINT(round(x), round(y)), 2)
        previous_context = None
        change_context = getattr(user32, "SetThreadDpiAwarenessContext", None)
        if full_monitor:
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            monitor = user32.MonitorFromWindow(widget.winfo_id(), 2)
            if change_context is not None:
                change_context.argtypes = [wintypes.HANDLE]
                change_context.restype = wintypes.HANDLE
                previous_context = change_context(ctypes.c_void_p(-4))
        try:
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect = info.monitor if full_monitor else info.work
                return rect.left, rect.top, rect.right, rect.bottom
        finally:
            if previous_context:
                change_context(previous_context)
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


def rounded(canvas, x1, y1, x2, y2, radius=16, **options):
    return canvas.create_polygon(
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        smooth=True, splinesteps=24, **options,
    )


class DesktopPet(tk.Toplevel):
    def __init__(self, master, image_paths, settings_path, on_restore, on_exit, *, animation_dirs=(), on_qq_settings=None, on_close_pet=None, on_check_updates=None):
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=TRANSPARENT)
        if os.name == "nt":
            self.attributes("-transparentcolor", TRANSPARENT)
            self.attributes("-toolwindow", True)
        self.on_restore, self.on_exit = on_restore, on_exit
        self.settings_path = Path(settings_path)
        self._visible = self._dragging = self._hovering = False
        self._size_dialog_open = False
        self._destroyed = False
        self._motion_job = self._bubble_job = None
        self._bubble_until = 0.0
        self._bubble_choice = None
        self.bubble_text = ""
        self._animation_dirs = tuple(Path(path) for path in animation_dirs)
        self._animations = {}
        self._animation_state = None
        self._sprite_item = None
        self._cached_work_area = None
        self._work_area_until = 0.0
        self._pointer_checked_at = 0.0
        self._source = Image.new("RGBA", (180, 180), "#ddecfa")
        for path in image_paths:
            try:
                with Image.open(path) as source:
                    self._source = ImageOps.exif_transpose(source).convert("RGBA")
                    self._source.thumbnail((MAX_SIZE, MAX_SIZE), Image.Resampling.LANCZOS)
                break
            except (OSError, ValueError):
                continue
        settings = self._load_settings()
        self.size = self._valid_size(settings.get("size", DEFAULT_SIZE))
        self.floating = tk.BooleanVar(self, value=settings.get("floating", True) is not False)
        left, top, right, bottom = work_area(self, 0, 0)
        self._x = self._coordinate(settings.get("x"), right - self.size - 60)
        self._y = self._coordinate(settings.get("y"), bottom - self.size - 60)
        self._clamp_position()
        self._anchor = (self._x, self._y)
        self._reset_float()
        self._last_frame = time.monotonic()
        self.canvas = tk.Canvas(self, width=self.size, height=self.size,
                                bg=TRANSPARENT, highlightthickness=0, cursor="hand2")
        self.canvas.pack()
        self._render_image()
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Double-Button-1>", self._double_click)
        self.canvas.bind("<Enter>", lambda _event: self._set_hover(True))
        self.canvas.bind("<Leave>", lambda _event: self._set_hover(False))
        self.canvas.bind("<Button-3>", self._popup_menu)
        self.menu = tk.Menu(self, tearoff=False)
        self.menu.bind("<Unmap>", lambda _event: self._end_menu())
        self.menu.add_command(label="打开聊天", command=on_restore)
        if on_check_updates is not None:
            self.menu.add_command(label="检查更新", command=on_check_updates)
        self._qq_menu_index = None
        if on_qq_settings is not None:
            self.menu.add_command(label="QQ 接入模式…", command=on_qq_settings)
            self._qq_menu_index = self.menu.index("end")
        sizes = tk.Menu(self.menu, tearoff=False)
        for label, size in (("迷你 · 80 px", 80), ("小 · 120 px", 120), ("中 · 180 px", 180), ("大 · 260 px", 260)):
            sizes.add_command(label=label, command=lambda value=size: self.set_size(value))
        sizes.add_separator()
        sizes.add_command(label="自定义大小…", command=self._custom_size)
        self.menu.add_cascade(label="宠物大小", menu=sizes)
        self.menu.add_checkbutton(label="缓慢漂浮", variable=self.floating, command=self._save_settings)
        self.menu.add_separator()
        if on_close_pet is not None:
            self.menu.add_command(label="关闭桌面宠物", command=on_close_pet)
        self.menu.add_command(label="退出", command=on_exit)
        self.bubble = tk.Toplevel(self)
        self.bubble.withdraw()
        self.bubble.overrideredirect(True)
        self.bubble.attributes("-topmost", True)
        self.bubble.configure(bg=TRANSPARENT)
        if os.name == "nt":
            self.bubble.attributes("-transparentcolor", TRANSPARENT)
            self.bubble.attributes("-toolwindow", True)
        self.bubble_canvas = tk.Canvas(self.bubble, width=260, height=105,
                                       bg=TRANSPARENT, highlightthickness=0, cursor="hand2")
        self.bubble_canvas.pack()

    @staticmethod
    def _valid_size(value):
        try:
            return max(MIN_SIZE, min(MAX_SIZE, int(value)))
        except (TypeError, ValueError, OverflowError):
            return DEFAULT_SIZE

    def set_qq_enabled(self, enabled):
        if self._qq_menu_index is not None:
            self.menu.entryconfigure(self._qq_menu_index, label="QQ 接入设置（已开启）…" if enabled else "QQ 接入模式…")

    @staticmethod
    def _coordinate(value, default):
        import math
        return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else float(default)

    def _load_settings(self):
        return self._read_settings(self.settings_path)

    @staticmethod
    def _read_settings(path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @classmethod
    def mode_enabled(cls, path):
        return cls._read_settings(path).get("enabled", True) is not False

    @classmethod
    def save_mode_enabled(cls, path, enabled):
        data = cls._read_settings(path)
        data["enabled"] = bool(enabled)
        cls._write_settings(Path(path), data)

    @staticmethod
    def _write_settings(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _save_settings(self):
        try:
            data = self._load_settings()
            data.update(size=self.size, x=round(self._anchor[0]),
                        y=round(self._anchor[1]), floating=self.floating.get())
            self._write_settings(self.settings_path, data)
        except OSError:
            logging.exception("Could not save desktop pet preferences")

    def _render_image(self):
        self._animations.clear()
        self._animation_state = None
        self.canvas.configure(width=self.size, height=self.size)
        self.canvas.delete("all")
        self._sprite_item = self.canvas.create_image(self.size / 2, self.size / 2)
        self._switch_animation(self.behavior.state)

    def _photo_frame(self, source):
        image = ImageOps.contain(source.convert("RGBA"), (self.size, self.size), Image.Resampling.LANCZOS)
        # A color-keyed Windows window supports binary alpha; keep edge colors clean.
        background = Image.new("RGB", image.size, TRANSPARENT)
        background.paste(image.convert("RGB"), mask=image.getchannel("A").point(lambda alpha: 255 if alpha >= 100 else 0))
        return ImageTk.PhotoImage(background, master=self)

    def _load_animation(self, state):
        for folder in self._animation_dirs:
            try:
                with Image.open(folder / f"{state}.gif") as source:
                    frames, delays = [], []
                    stride = max(1, (source.n_frames + 179) // 180)
                    for index in range(source.n_frames):
                        source.seek(index)
                        duration = max(20, min(2000, int(source.info.get("duration", 100))))
                        if index % stride == 0:
                            frames.append(self._photo_frame(source))
                            delays.append(duration)
                        else:
                            delays[-1] += duration
                if frames:
                    return frames, delays
            except (OSError, ValueError, Image.DecompressionBombError):
                continue
        return [self._photo_frame(self._source)], [1000]

    def _switch_animation(self, state):
        if self._animation_state == state:
            return
        if state not in self._animations:
            self._animations[state] = self._load_animation(state)
        self._frames, self._frame_delays = self._animations[state]
        self._animation_state, self._frame_index = state, 0
        self._next_sprite_at = time.monotonic() + self._frame_delays[0] / 1000
        self._image = self._frames[0]
        self.canvas.itemconfigure(self._sprite_item, image=self._image)

    def _animate_sprite(self, now):
        if len(self._frames) > 1 and now >= self._next_sprite_at:
            self._frame_index = (self._frame_index + 1) % len(self._frames)
            self._image = self._frames[self._frame_index]
            self.canvas.itemconfigure(self._sprite_item, image=self._image)
            self._next_sprite_at = now + self._frame_delays[self._frame_index] / 1000

    def _clamp_position(self):
        left, top, right, bottom = work_area(self, self._x + self.size / 2, self._y + self.size / 2)
        self._cached_work_area = left, top, right, bottom
        self._work_area_until = time.monotonic() + 1.0
        self._x = max(left, min(self._x, right - self.size))
        self._y = max(top, min(self._y, bottom - self.size))

    def _place(self):
        position_window(self, self.size, self.size, self._x, self._y)
        if self.has_bubble:
            self._position_bubble()

    def show(self):
        old_position = (self._x, self._y)
        self._clamp_position()
        if old_position != (self._x, self._y):
            self._anchor = (self._x, self._y)
            self._reset_float()
        self._place()
        self._visible = True
        self._last_frame = time.monotonic()
        self.deiconify()
        self.lift()
        if self._motion_job is None:
            self._motion_job = self.after(40, self._float_tick)

    def hide(self):
        self._visible = False
        self._dragging = self._hovering = False
        self._hide_bubble()
        if self._motion_job is not None:
            self.after_cancel(self._motion_job)
            self._motion_job = None
        self._save_settings()
        self.withdraw()

    def set_size(self, size):
        center_x, bottom_y = self._x + self.size / 2, self._y + self.size
        self.size = self._valid_size(size)
        self._x, self._y = center_x - self.size / 2, bottom_y - self.size
        self._clamp_position()
        self._anchor = (self._x, self._y)
        self._reset_float()
        self._render_image()
        self._place()
        self._save_settings()

    def _custom_size(self):
        self._size_dialog_open = True
        try:
            size = simpledialog.askinteger("宠物大小", f"输入边长（{MIN_SIZE}–{MAX_SIZE} 像素）",
                                           initialvalue=self.size, minvalue=MIN_SIZE,
                                           maxvalue=MAX_SIZE, parent=self)
        finally:
            self._size_dialog_open = False
        if size is not None:
            self.set_size(size)

    def _popup_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _end_menu(self):
        self._resume_motion_at = time.monotonic() + .4

    def _set_hover(self, hovering):
        self._hovering = hovering

    def _drag_start(self, event):
        self._dragging = True
        self._drag_origin = (event.x_root, event.y_root, self._x, self._y)

    def _drag_move(self, event):
        if self._dragging:
            start_x, start_y, x, y = self._drag_origin
            self._x, self._y = x + event.x_root - start_x, y + event.y_root - start_y
            self._clamp_position()
            self._place()

    def _drag_end(self, _event=None):
        if self._dragging:
            self._dragging = False
            self._anchor = (self._x, self._y)
            self._reset_float(delay=.6)
            self._save_settings()

    def _double_click(self, _event=None):
        self._drag_end()
        self.on_restore()
        return "break"

    def _reset_float(self, delay=0):
        self.behavior = PetBehavior()
        self._resume_motion_at = time.monotonic() + delay
        if self._sprite_item is not None:
            self._switch_animation("relax")

    def _pointer_over_pet(self):
        # Enter/Leave can be missed when menus or transparent windows change focus.
        try:
            widget = self.winfo_containing(*self.winfo_pointerxy())
            return widget is self or widget is self.canvas
        except tk.TclError:
            return False

    def _float_tick(self):
        self._motion_job = None
        if not self._visible:
            return
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_frame)
        self._last_frame = now
        if self.behavior.state == "float" or now - self._pointer_checked_at >= .2:
            self._hovering = self._pointer_over_pet()
            self._pointer_checked_at = now
        paused = (self._dragging or self._hovering or self.menu.winfo_ismapped()
                  or self._size_dialog_open or self.has_bubble or now < self._resume_motion_at)
        ax, ay = self._anchor
        if self._cached_work_area is None or now >= self._work_area_until:
            self._cached_work_area = work_area(self, ax + self.size / 2, ay + self.size / 2)
            self._work_area_until = now + 1.0
        left, top, right, bottom = self._cached_work_area
        bounds = (max(-FLOAT_RADIUS_X, left - ax), max(-FLOAT_RADIUS_Y, top - ay),
                  min(FLOAT_RADIUS_X, right - self.size - ax), min(FLOAT_RADIUS_Y, bottom - self.size - ay))
        self.behavior.advance(elapsed, paused=paused, allow_float=self.floating.get(), bounds=bounds)
        self._switch_animation(self.behavior.state)
        self._animate_sprite(now)
        if not paused:
            old_position = (round(self._x), round(self._y))
            self._x, self._y = ax + self.behavior.x, ay + self.behavior.y
            if old_position != (round(self._x), round(self._y)):
                self._place()
        self._motion_job = self.after(self._next_tick_ms(now, paused), self._float_tick)

    def _next_tick_ms(self, now, paused):
        # Only moving needs 25 fps. Rest/play follow actual GIF frame deadlines.
        if self.behavior.state == "float" and not paused:
            return 40
        wait = 1000
        if len(self._frames) > 1:
            wait = min(wait, max(20, round((self._next_sprite_at - now) * 1000)))
        if self.behavior.state == "play":
            deadline = self.behavior.play_until
        else:
            deadline = min(self.behavior.next_float, self.behavior.next_play)
        if deadline > self.behavior.clock:
            wait = min(wait, max(20, round((deadline - self.behavior.clock) * 1000)))
        return wait

    @property
    def has_bubble(self):
        return bool(self.bubble_text) and time.monotonic() < self._bubble_until

    def show_bubble(self, text, *, preview=True):
        if not self._visible:
            return
        self._hide_bubble()
        self.bubble_text = message_preview(text) if preview else text
        self._bubble_until = time.monotonic() + BUBBLE_SECONDS
        self._position_bubble()
        self.bubble.deiconify()
        self.bubble.lift()
        if self._bubble_job is not None:
            self.after_cancel(self._bubble_job)
        self._bubble_job = self.after(BUBBLE_SECONDS * 1000, self._hide_bubble)

    def show_question(self, text, on_answer):
        if not self._visible:
            return
        self._hide_bubble()
        self.bubble_text, self._bubble_choice = text, on_answer
        self._bubble_until = time.monotonic() + QUESTION_SECONDS
        self._position_bubble()
        self.bubble.deiconify()
        self.bubble.lift()
        self._bubble_job = self.after(QUESTION_SECONDS * 1000, self._hide_bubble)

    def _answer_bubble(self, accepted):
        callback = self._bubble_choice if self.has_bubble else None
        self._hide_bubble()
        if callback is not None:
            callback(accepted)
        return "break"

    def _position_bubble(self):
        question = self._bubble_choice is not None
        width, height = 260, 150 if question else 105
        self.bubble_canvas.configure(width=width, height=height)
        left, top, right, bottom = work_area(self, self._x + self.size / 2, self._y + self.size / 2)
        x = max(left, min(self._x + self.size / 2 - width / 2, right - width))
        above = self._y - height - 6 >= top
        y = self._y - height - 6 if above else min(bottom - height, self._y + self.size + 6)
        position_window(self.bubble, width, height, x, y)
        canvas = self.bubble_canvas
        canvas.delete("all")
        y1, y2 = (2, height - 17) if above else (14, height - 5)
        rounded(canvas, 3, y1 + 2, 257, y2 + 2, fill="#cbdfea", outline="")
        rounded(canvas, 3, y1, 257, y2, fill="#f5faff", outline="#d5e6f3")
        tip = max(24, min(236, self._x + self.size / 2 - x))
        canvas.create_polygon(tip - 8, y2 - 1 if above else y1 + 1,
                              tip + 8, y2 - 1 if above else y1 + 1,
                              tip, height - 4 if above else 2, fill="#f5faff", outline="")
        canvas.create_text(20, y1 + 16, text=self.bubble_text, anchor="nw", width=220,
                           font=(FONT, 10), fill="#29485e")
        if question:
            canvas.create_text(20, y1 + 44, text="同意后截图并发送给 AI，仅限本次", anchor="nw", font=(FONT, 8), fill="#88a1b3")
            for label, accepted, x1 in (("是", True, 20), ("否", False, 137)):
                tag = "accept" if accepted else "decline"
                rounded(canvas, x1, y1 + 73, x1 + 100, y1 + 106, radius=9,
                        fill="#529bd3" if accepted else "#e1ecf4", outline="", tags=tag)
                canvas.create_text(x1 + 50, y1 + 89, text=label, font=(FONT, 10),
                                   fill="white" if accepted else "#29485e", tags=tag)
                canvas.tag_bind(tag, "<Button-1>", lambda _event, choice=accepted: self._answer_bubble(choice))
        else:
            canvas.create_text(238, y2 - 12, text="点击查看", anchor="e", font=(FONT, 8), fill="#88a1b3")
            canvas.addtag_all("bubble_open")
            canvas.tag_bind("bubble_open", "<Button-1>", lambda _event: self.on_restore())

    def _hide_bubble(self):
        if self._bubble_job is not None:
            self.after_cancel(self._bubble_job)
            self._bubble_job = None
        self._bubble_until = 0
        self._bubble_choice = None
        self.bubble_text = ""
        self.bubble.withdraw()

    def destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        for job in (self._motion_job, self._bubble_job):
            if job is not None:
                self.after_cancel(job)
        super().destroy()
        self._animations.clear()
        self._frames = []
        self._image = None
        self.floating = None
        self.on_restore = self.on_exit = None
        for name, value in tuple(vars(self).items()):
            if isinstance(value, (tk.Misc, tk.Variable)):
                setattr(self, name, None)
