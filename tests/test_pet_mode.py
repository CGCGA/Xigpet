import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from xigpet import app
from xigpet.system_tray import SystemTray
import test_pet_core as core_tests


class PetModeTests(unittest.TestCase):
    setUp = core_tests.PetCoreTests.setUp

    def window(self):
        with patch.object(app.PetWindow, "_schedule_tick"):
            window = app.PetWindow()
        self.addCleanup(window.destroy)
        window._tray = MagicMock()
        window._tray.ready = threading.Event()
        window._tray.ready.set()
        return window

    def test_close_menu_releases_pet_keeps_qq_and_blocks_bubbles(self):
        window = self.window()
        bridge = window._qq_bridge = MagicMock()
        window._minimize_to_tray()
        pet = window._desktop
        pet.show_question(app.SCREEN_REQUEST_TEXT, lambda accepted: None)
        token = window._active_screen_request = object()
        menu_index = next(index for index in range(pet.menu.index("end") + 1)
                          if pet.menu.type(index) == "command"
                          and pet.menu.entrycget(index, "label") == "关闭桌面宠物")
        pet.menu.invoke(menu_index)
        self.assertTrue(pet._destroyed)
        self.assertEqual(pet._animations, {})
        self.assertIsNone(window._desktop)
        self.assertFalse(window._desktop_enabled)
        self.assertIsNone(window._active_screen_request)
        self.assertTrue(window._background)
        self.assertEqual(window.state(), "withdrawn")
        self.assertIs(window._qq_bridge, bridge)
        bridge.stop.assert_not_called()
        with patch.object(app.random, "choice") as choose, patch.object(app.ImageGrab, "grab") as grab:
            window._next_idle_at = 0
            window._desktop_tick()
            window._on_screen_answer(token, True)
            choose.assert_not_called()
            grab.assert_not_called()
        window._notify_background_message("后台回复仍然送达。")
        self.assertIsNone(window._desktop)
        window._tray.notify.assert_called_once()
        window.events.put(("qq_send_error", (bridge, "fixture failure")))
        window.events.put(("idle_drawing_error", "fixture failure"))
        window._drain_events()
        self.assertIsNone(window._desktop)

    def test_disabled_mode_persists_and_tray_can_enable_it_again(self):
        window = self.window()
        window._minimize_to_tray()
        window._desktop.set_size(120)
        window._set_desktop_enabled(False)
        config_path = app.pet_core.KEY_FILE.parent / "desktop_pet.json"
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["size"], 120)
        restarted = self.window()
        self.assertFalse(restarted._desktop_enabled)
        restarted._minimize_to_tray()
        self.assertIsNone(restarted._desktop)
        self.assertEqual(restarted.state(), "withdrawn")
        restarted.events.put(("tray_toggle_pet", ""))
        restarted._drain_events()
        self.assertTrue(restarted._desktop_enabled)
        self.assertEqual(restarted._desktop.size, 120)
        self.assertEqual(restarted._desktop.state(), "normal")
        restarted._desktop.hide()
        self.assertTrue(app.DesktopPet.mode_enabled(config_path))
        restarted.events.put(("tray_restore", ""))
        restarted._drain_events()
        self.assertEqual(restarted.state(), "normal")
        restarted.pet_mode_button.invoke()
        self.assertFalse(restarted._desktop_enabled)
        restarted.pet_mode_button.invoke()
        self.assertTrue(restarted._desktop_enabled)

    def test_missing_tray_restores_chat_instead_of_losing_all_entry_points(self):
        window = self.window()
        window._minimize_to_tray()
        window._tray.ready.clear()
        window._set_desktop_enabled(False)
        self.assertFalse(window._background)
        self.assertEqual(window.state(), "normal")
        window._minimize_to_tray()
        with patch.object(app.logging, "warning"):
            window._tray_failed("fixture failure")
        self.assertFalse(window._background)
        self.assertEqual(window.state(), "normal")
        self.assertIsNone(window._desktop)

    def test_tray_callbacks_only_queue_ui_actions(self):
        tray = SystemTray("fixture", [], app.queue.Queue())
        self.addCleanup(tray.stop)
        tray.set_pet_enabled(False)
        items = list(tray.icon.menu)
        pet_item = next(item for item in items if item.text == "桌面宠物")
        self.assertFalse(pet_item.checked)
        tray._toggle_pet(None, None)
        tray._qq_settings(None, None)
        self.assertEqual(tray.events.get_nowait()[0], "tray_toggle_pet")
        self.assertEqual(tray.events.get_nowait()[0], "tray_qq_settings")
        tray.set_pet_enabled(True)
        self.assertTrue(pet_item.checked)


if __name__ == "__main__":
    unittest.main()
