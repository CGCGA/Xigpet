import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xigpet import app
from xigpet import pet_core
import test_pet_core as core_tests
from xigpet.chat_history import HistoryArchive, UNKNOWN_DATE
from xigpet.chat_view import ConversationMixin, MEDIA_CACHE_BYTES, MEDIA_CACHE_ITEMS
from xigpet.memory_catalog import MemoryCatalog


def record(number, day="2026-10-22", **extra):
    return {"time": f"{day}T12:00:00+08:00", "role": "assistant" if number % 2 else "user",
            "content": f"消息 {number}，你好！", **extra}


class HistoryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.catalog = MemoryCatalog(root / "history.jsonl", root / "catalog.jsonl", root / "state.json")
        self.archive = HistoryArchive(self.catalog)

    def write(self, rows):
        self.catalog.history.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def test_recent_fifty_cross_dates_preserves_all_history_and_tool_limit(self):
        rows = [record(i, "2026-10-21" if i < 90 else "2026-10-22") for i in range(110)]
        self.write(rows)
        before = self.catalog.history.read_bytes()
        self.assertEqual(self.archive.recent(), rows[-50:])
        self.assertEqual(self.catalog.history.read_bytes(), before)
        self.assertEqual(self.archive.recent(0), [])
        self.assertEqual(self.catalog.read_lines(1, 50), [])  # The agent's existing 20-line gate.

    def test_tail_handles_block_boundaries_invalid_rows_and_no_final_newline(self):
        rows = [record(i, content="中文字符🙂" * 5000 + str(i)) for i in range(70)]
        self.write(rows)
        with self.catalog.history.open("ab") as stream:
            stream.write(b"broken json\n[]\n{\"role\":\"system\",\"content\":\"skip\"}\n")
            stream.write(json.dumps(record(70), ensure_ascii=False).encode("utf-8"))
        self.assertEqual(self.archive.recent(), [*rows[-49:], record(70)])

    def test_dates_pages_append_and_index_rebuild(self):
        rows = [record(i, "2026-10-20" if i < 115 else "2026-10-22") for i in range(125)]
        self.write(rows)
        self.assertEqual(self.archive.dates(), [("2026-10-22", 10), ("2026-10-20", 115)])
        self.assertEqual(self.archive.page("2026-10-20", 1), (rows[50:100], 115, 1))
        self.assertEqual(self.archive.page("2026-10-20", 99), (rows[100:115], 115, 2))
        self.catalog.append("assistant", "新消息", "2026-10-22T23:59:00+08:00")
        self.assertEqual(self.archive.dates()[0], ("2026-10-22", 11))
        self.catalog.label(1, "修改目录标题", "测试")
        self.assertEqual(self.archive.dates()[0], ("2026-10-22", 11))
        self.write([record(999, "2026-10-19")])
        self.assertEqual(self.archive.dates(), [("2026-10-19", 1)])
        self.assertEqual(self.archive.page("2026-10-20"), ([], 0, 0))

    def test_empty_and_unknown_date_records_remain_accessible(self):
        self.assertEqual(self.archive.recent(), [])
        self.assertEqual(self.archive.dates(), [])
        self.write([record(0, time="not-a-date"), record(1)])
        self.assertEqual(self.archive.dates(), [("2026-10-22", 1), (UNKNOWN_DATE, 1)])
        self.assertEqual(self.archive.page(UNKNOWN_DATE)[0][0]["content"], record(0)["content"])

    def test_unchanged_dates_reuse_index_and_append_does_not_duplicate(self):
        self.write([record(i) for i in range(100)])
        self.archive.dates()
        bucket = self.archive._days["2026-10-22"]
        self.archive.dates()
        self.assertIs(self.archive._days["2026-10-22"], bucket)
        self.catalog.append("assistant", "追加", "2026-10-22T13:00:00")
        self.assertEqual(self.archive.dates(), [("2026-10-22", 101)])
        self.assertIs(self.archive._days["2026-10-22"], bucket)
        self.assertEqual(len(self.archive._days["2026-10-22"]), 101)

    def test_concurrent_append_and_browse_do_not_corrupt_index(self):
        self.write([record(0)])
        errors = []
        other = MemoryCatalog(self.catalog.history, self.catalog.catalog, self.catalog.state)
        self.assertIs(other._lock, self.catalog._lock)
        def writer():
            try:
                for i in range(60):
                    other.append("assistant", str(i), "2026-10-22T13:00:00")
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=writer)
        thread.start()
        for _ in range(15):
            self.archive.dates()
            rows, total, _ = self.archive.page("2026-10-22")
            self.assertEqual(len(rows), min(total, 50))
        thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.archive.dates(), [("2026-10-22", 61)])


class HistoryWindowTests(unittest.TestCase):
    setUp = core_tests.PetCoreTests.setUp

    def window(self):
        self.addCleanup(patch.stopall)
        patch.object(app.PetWindow, "_schedule_tick", return_value=None).start()
        patch.object(pet_core, "call_deepseek", side_effect=AssertionError("No model calls while browsing")).start()
        window = app.PetWindow()
        self.addCleanup(window.destroy)
        return window

    def write(self, rows):
        pet_core.HISTORY.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def pump(self, window, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            window.update()
            if predicate():
                return
            time.sleep(.005)
        self.fail("History UI did not finish loading")

    def test_main_page_limits_live_and_restored_messages_without_deleting_history(self):
        rows = [record(i, "2026-10-21" if i < 75 else "2026-10-22") for i in range(100)]
        self.write(rows)
        window = self.window()
        window.update_idletasks()
        self.assertEqual([row["content"] for row in window.messages], [row["content"] for row in rows[-50:]])
        self.assertEqual(len(window.chat_canvas.find_withtag("message_text")), 50)
        for i in range(100, 240):
            window._add_message("assistant", f"新消息 {i}")
        window.update_idletasks()
        self.assertEqual(len(window.messages), 50)
        self.assertEqual(window.messages[0]["content"], "新消息 190")
        self.assertEqual(len(window.chat_canvas.find_withtag("message_text")), 50)
        self.assertEqual(len(pet_core.HISTORY.read_text(encoding="utf-8").splitlines()), 100)

    def test_history_dates_filter_day_pages_refresh_and_read_only_shared_style(self):
        rows = [record(i, "2026-10-21" if i < 115 else "2026-10-22") for i in range(125)]
        self.write(rows)
        window = self.window()
        window._remove_placeholder()
        window.input.insert("1.0", "保留的草稿")
        window._open_history()
        history = window._history_window
        self.pump(window, lambda: not history._loading)
        self.assertEqual(history.date_list.get_children(), ("2026-10-22", "2026-10-21"))
        history.date_filter.set("10-21")
        self.assertEqual(history.date_list.get_children(), ("2026-10-21",))
        history.open_selected()
        self.pump(window, lambda: not history._loading)
        self.assertIsInstance(history.conversation, ConversationMixin)
        self.assertEqual(history.conversation.messages, rows[:50])
        self.assertFalse(hasattr(history.conversation, "input"))
        self.assertEqual(history.next_button.cget("state"), "normal")
        history.next_button.invoke()
        self.pump(window, lambda: not history._loading)
        self.assertEqual(history.conversation.messages, rows[50:100])
        history.next_button.invoke()
        self.pump(window, lambda: not history._loading)
        self.assertEqual(history.conversation.messages, rows[100:115])
        self.assertEqual(history.next_button.cget("state"), "disabled")
        pet_core.memory_catalog().append("assistant", "同一天的新回复", "2026-10-21T23:59:00")
        history.refresh()
        self.pump(window, lambda: not history._loading)
        self.assertEqual(history.total, 116)
        self.assertEqual(history.conversation.messages[-1]["content"], "同一天的新回复")
        history.show_dates()
        self.pump(window, lambda: not history._loading)
        self.assertEqual(window.input.get("1.0", "end-1c"), "保留的草稿")
        self.assertEqual(len(window.messages), 50)
        self.assertEqual(history.conversation.messages, [])
        history.destroy()
        window._open_history()
        self.assertIsNot(history, window._history_window)
        self.pump(window, lambda: not window._history_window._loading)

    def test_history_shows_images_timestamps_and_source_links(self):
        image = core_tests.PetCoreTests._shared_image(self)
        import shutil
        shutil.copy2(pet_core.ROOT / "media" / "wink.gif", pet_core.MEDIA_DIR / "wink.gif")
        rows = [record(0, content=f"看看图片\n[[user-image:{image}]]"),
                record(1, content="这是来源 https://example.com/test\n[[pet-media:wink.gif]]")]
        self.write(rows)
        window = self.window()
        window._open_history()
        history = window._history_window
        self.pump(window, lambda: not history._loading)
        history.load_day("2026-10-22")
        self.pump(window, lambda: not history._loading)
        window.update_idletasks()
        history.conversation._animate_media()
        canvas = history.conversation.chat_canvas
        self.assertEqual(len(canvas.find_withtag("chat_media")), 2)
        self.assertEqual(len(canvas.find_withtag("source_link")), 1)
        self.assertTrue(canvas.find_withtag("message_time"))
        self.assertGreater(len(history.conversation.animated_items[0]["frames"]), 1)

    def test_background_defers_layout_and_restores_only_latest_fifty(self):
        window = self.window()
        window.update_idletasks()
        window._background = True
        with patch.object(window, "_draw_message") as render:
            for i in range(90):
                window._add_message("assistant", str(i))
            window.update_idletasks()
            render.assert_not_called()
        self.assertTrue(window._layout_dirty)
        window._restore_from_tray()
        window.update_idletasks()
        self.assertFalse(window._layout_dirty)
        self.assertEqual(len(window.messages), 50)
        self.assertEqual(window.messages[0]["content"], "40")
        self.assertAlmostEqual(window.chat_canvas.yview()[1], 1.0, places=2)

    def test_cache_is_bounded_and_offscreen_gifs_are_not_decoded(self):
        import shutil
        shutil.copy2(pet_core.ROOT / "media" / "wink.gif", pet_core.MEDIA_DIR / "wink.gif")
        window = self.window()
        for i in range(55):
            name = f"test_{i}.png"
            app.Image.new("RGB", (220, 200), "blue").save(pet_core.MEDIA_DIR / name)
            window._add_message("assistant", f"[[pet-media:{name}]]")
        window.update_idletasks()
        self.assertLessEqual(len(window.media_images), MEDIA_CACHE_ITEMS)
        self.assertLessEqual(sum(window._media_costs.values()), MEDIA_CACHE_BYTES)
        for i in range(50):
            window._add_message("assistant", "[[pet-media:wink.gif]]")
        window.update()
        window._animate_media()
        loaded = [item for item in window.animated_items if item["loaded"]]
        self.assertGreater(len(loaded), 0)
        self.assertLessEqual(len(loaded), 3)
        self.assertTrue(all(len(item["frames"]) == 1 for item in window.animated_items if not item["loaded"]))
        window._release_animated_media()
        self.assertEqual(len(window.media_images), 0)
        self.assertTrue(all(not item["loaded"] for item in window.animated_items))

    def test_desktop_uses_slow_idle_ticks_and_cached_monitor_bounds(self):
        from xigpet.desktop_pet import DesktopPet
        window = self.window()
        pet = DesktopPet(window, (pet_core.ROOT / "Xig.png",),
                         pet_core.KEY_FILE.parent / "desktop_pet.json", lambda: None, lambda: None,
                         animation_dirs=(pet_core.ROOT / "animation",))
        self.addCleanup(pet.destroy)
        now = time.monotonic()
        pet._next_sprite_at = now + .36
        self.assertGreaterEqual(pet._next_tick_ms(now, False), 250)
        pet.behavior.state = "float"
        self.assertEqual(pet._next_tick_ms(now, False), 40)
        self.assertGreaterEqual(pet._next_tick_ms(now, True), 250)
        pet.behavior.state = "relax"
        pet._visible = True
        pet._work_area_until = now + 10
        with patch("xigpet.desktop_pet.work_area") as query:
            for _ in range(5):
                pet._float_tick()
                pet.after_cancel(pet._motion_job)
                pet._motion_job = None
            query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
