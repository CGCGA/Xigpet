import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts.drawing_tools import draw_picture
from scripts.media_tools import attach_media, media_inventory, media_path, search_media, split_media
from xigpet import pet_core
from xigpet.qq_bridge import outgoing_segments
from xigpet.storage_layout import migrate_layout
import test_pet_core as core_tests


class StorageMigrationTests(unittest.TestCase):
    def test_migrates_setting_notes_and_old_art_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pet").mkdir()
            setting_bytes = "你叫小希。\r\n保留原格式。".encode("utf-8")
            (root / "pet" / "setting.md").write_bytes(setting_bytes)
            (root / "notes").mkdir()
            (root / "notes" / "todo.md").write_text("吃饭", encoding="utf-8")
            media = root / "media"
            media.mkdir()
            legacy_name = "drawing_12345678.png"
            Image.new("RGB", (8, 8), "blue").save(media / legacy_name)
            image_bytes = (media / legacy_name).read_bytes()
            history = root / "memory" / "chat_history.jsonl"
            history.parent.mkdir()
            rows = [{"role": "assistant", "content": "我画了蓝色的小星星。"},
                    {"role": "assistant", "content": attach_media("", [legacy_name])}]
            history.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
            original_history = history.read_bytes()
            setting, notes = root / "config" / "setting.md", root / "memory" / "notes"
            migrate_layout(setting, notes, media, history)
            migrate_layout(setting, notes, media, history)
            self.assertEqual(setting.read_bytes(), setting_bytes)
            self.assertEqual((notes / "todo.md").read_text(encoding="utf-8"), "吃饭")
            self.assertFalse((root / "pet").exists())
            self.assertFalse((root / "notes").exists())
            self.assertEqual(history.read_bytes(), original_history)
            self.assertEqual(media_path(media, legacy_name).read_bytes(), image_bytes)
            self.assertEqual(media_path(media, legacy_name), media / "drawings" / legacy_name)
            result = json.loads(search_media(media, "星星"))[0]
            self.assertEqual(result["file"], "drawings/" + legacy_name)
            self.assertEqual(result["title"], "蓝色的小星星")
            segments = outgoing_segments(attach_media("旧画", [legacy_name]), media)
            self.assertEqual(base64.b64decode(segments[-1]["data"]["file"][9:]), image_bytes)

    def test_existing_setting_and_notes_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("pet", "config", "notes", "memory/notes", "media"):
                (root / folder).mkdir(parents=True)
            for path, text in (("pet/setting.md", "old"), ("config/setting.md", "new"),
                               ("notes/todo.md", "old note"), ("memory/notes/todo.md", "new note")):
                (root / path).write_text(text, encoding="utf-8")
            migrate_layout(root / "config/setting.md", root / "memory/notes", root / "media", root / "memory/history.jsonl")
            self.assertEqual((root / "config/setting.md").read_text(), "new")
            self.assertEqual((root / "memory/notes/todo.md").read_text(), "new note")
            self.assertEqual((root / "pet/setting.md").read_text(), "old")
            self.assertEqual((root / "notes/todo.md").read_text(), "old note")


class DrawingLibraryTests(unittest.TestCase):
    setUp = core_tests.PetCoreTests.setUp

    def draw(self, title="晚安小星星", **kwargs):
        return draw_picture(pet_core.MEDIA_DIR, title=title, code="draw.ellipse((8,8,80,80),fill='blue')",
                            width=256, height=256, **kwargs)

    def test_named_drawing_is_saved_indexed_and_searchable_after_new_session(self):
        stock_index = pet_core.MEDIA_DIR / "index.json"
        original_stock = b'{"stock.png":{"title":"stock"}}'
        stock_index.write_bytes(original_stock)
        result = self.draw(keywords="星空 月亮 星星", mood="晚安 安慰")
        path = media_path(pet_core.MEDIA_DIR, result["sent"])
        self.assertEqual(path.parent, pet_core.MEDIA_DIR / "drawings")
        self.assertIn("晚安小星星", path.name)
        with Image.open(path) as image:
            self.assertEqual(image.info["Title"], "晚安小星星")
        index = json.loads((path.parent / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index[path.name]["mood"], "晚安 安慰")
        self.assertEqual(index[path.name]["keywords"], "星空 月亮 星星")
        self.assertEqual(stock_index.read_bytes(), original_stock)
        for query in ("晚安小星星", "月亮", "安慰", "以前画过的作品"):
            with self.subTest(query=query):
                found = json.loads(search_media(pet_core.MEDIA_DIR, query))
                self.assertEqual(found[0]["file"], result["sent"])
        self.assertEqual(pet_core.read_history(), [])

    def test_later_chat_can_resend_its_art_without_drawing_again(self):
        art = self.draw(keywords="星星", mood="晚安")
        def tool(name, arguments):
            return {"tool_calls": [{"id": name, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments),
            }}]}
        responses = [tool("search_media", {"query": "晚安小星星"}),
                     tool("send_media", {"filename": art["sent"], "purpose": "requested"}),
                     {"content": "这就是我之前画的小星星。"}, core_tests.continuation()]
        with patch.object(pet_core, "call_deepseek", side_effect=responses), \
             patch.object(pet_core.drawing_tools, "draw_picture") as draw:
            answer = pet_core.reply("把你以前画的星星再发给我")
            draw.assert_not_called()
        self.assertEqual(split_media(answer)[1], [art["sent"]])
        segments = outgoing_segments(answer, pet_core.MEDIA_DIR)
        self.assertEqual(base64.b64decode(segments[-1]["data"]["file"][9:]),
                         media_path(pet_core.MEDIA_DIR, art["sent"]).read_bytes())
        self.assertIn(art["sent"], pet_core.read_history()[-1]["content"])

    def test_inventory_reserves_space_for_drawings_and_paths_cannot_escape(self):
        first = self.draw()
        for index in range(25):
            Image.new("RGB", (8, 8)).save(pet_core.MEDIA_DIR / f"xig_{index}.png")
        inventory = media_inventory(pet_core.MEDIA_DIR)
        self.assertLessEqual(len(inventory), 24)
        self.assertTrue(any(item["file"] == first["sent"] for item in inventory))
        for filename in ("../config/deepseek_key.txt", "drawings/../stock.png", "drawings\\test.png",
                         "unknown/test.png", "drawings/../../test.png", "/test.png"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                media_path(pet_core.MEDIA_DIR, filename)


if __name__ == "__main__":
    unittest.main()
