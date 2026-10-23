"""Windows tray callbacks communicate with Tk through its event queue."""

from __future__ import annotations

import threading

from PIL import Image, ImageOps


class SystemTray:
    def __init__(self, name, image_paths, events):
        import pystray

        self._closed = threading.Event()
        self.ready = threading.Event()
        self.events = events
        self._pet_enabled = True
        image = Image.new("RGBA", (64, 64), "#ddecfa")
        for path in image_paths:
            try:
                with Image.open(path) as source:
                    image = ImageOps.pad(source.convert("RGBA"), (64, 64))
                break
            except (OSError, ValueError):
                continue
        self.icon = pystray.Icon(
            "Xigpet", image, name,
            menu=pystray.Menu(
                pystray.MenuItem("打开聊天", self._restore, default=True),
                pystray.MenuItem("桌面宠物", self._toggle_pet, checked=lambda _item: self._pet_enabled),
                pystray.MenuItem("QQ 接入设置…", self._qq_settings),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._exit),
            ),
        )

    def start(self):
        self.icon.run_detached(self._setup)

    def _setup(self, icon):
        try:
            if self._closed.is_set():
                icon.stop()
                return
            icon.visible = True
            self.ready.set()
            if self._closed.is_set():
                icon.stop()
            else:
                self.events.put(("tray_ready", ""))
        except Exception as exc:
            icon.stop()
            self.events.put(("tray_error", str(exc)))

    def _restore(self, _icon, _item):
        self.events.put(("tray_restore", ""))

    def _exit(self, _icon, _item):
        self.events.put(("tray_exit", ""))

    def _toggle_pet(self, _icon, _item):
        self.events.put(("tray_toggle_pet", ""))

    def _qq_settings(self, _icon, _item):
        self.events.put(("tray_qq_settings", ""))

    def set_pet_enabled(self, enabled):
        self._pet_enabled = bool(enabled)
        if self.ready.is_set():
            self.icon.update_menu()

    def set_title(self, title):
        self.icon.title = title[:127]

    def notify(self, title, text):
        if self.ready.is_set() and self.icon.HAS_NOTIFICATION:
            try:
                self.icon.notify(text[:180], title[:63])
            except (OSError, RuntimeError):
                pass

    def clear_notification(self):
        if self.ready.is_set() and self.icon.HAS_NOTIFICATION:
            try:
                self.icon.remove_notification()
            except (OSError, RuntimeError):
                pass

    def stop(self):
        self._closed.set()
        self.icon.stop()
