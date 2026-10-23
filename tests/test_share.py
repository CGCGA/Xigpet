import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import prepare_share as share
from xigpet import pet_core
import test_pet_core as core_tests


class SharePrivacyTests(unittest.TestCase):
    def test_allowlist_excludes_private_files_and_resets_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [*share.TOP_FILES, *("animation/" + name + ".gif" for name in ("relax", "float", "play")),
                     *("media/" + name for name in share.MEDIA), "xigpet/app.py", "scripts/tool.py", "tests/test_dummy.py"]
            for name in files:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"public fixture")
            private = {
                "config/deepseek_key.txt": "fixture-private-key-123456789",
                "config/qq_bridge.json": json.dumps({"access_token": "fixture-private-token-987654321"}),
                "config/setting.md": "personal character fixture",
                "memory/chat_history.jsonl": "private conversation fixture",
                "memory/images/screen.png": "private screen fixture",
                "media/drawings/private.png": "private drawing fixture",
                "plans/plan.md": "private schedule fixture",
                "notes/private.md": "private note fixture",
                "Xigpet.exe": "old executable fixture",
                "build/private.bin": "build artifact fixture",
            }
            for name, text in private.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            output = root / "tem"
            result = share.prepare_share(root, output)
            self.assertGreater(result["files"], 20)
            self.assertEqual((output / "config/deepseek_key.txt").read_bytes(), b"")
            self.assertEqual((output / "config/setting.md").read_text(encoding="utf-8"), share.SETTING_EXAMPLE)
            for name in private:
                if name not in ("config/deepseek_key.txt", "config/setting.md"):
                    self.assertFalse((output / name).exists(), name)
            manifest = json.loads((output / "SHARE_MANIFEST.json").read_text(encoding="utf-8"))
            self.assertNotIn("config/deepseek_key.txt", manifest)
            self.assertNotIn("config/setting.md", manifest)
            for value in private.values():
                self.assertFalse(any(value.encode("utf-8") in p.read_bytes() for p in output.rglob("*") if p.is_file()))
            with self.assertRaises(FileExistsError):
                share.prepare_share(root, output)
            (output / "scripts/tool.py").write_text(private["config/deepseek_key.txt"])
            with self.assertRaisesRegex(ValueError, "隐私检查"):
                share.audit_share(root, output)


class ShareStartupTests(unittest.TestCase):
    setUp = core_tests.PetCoreTests.setUp

    def test_persona_example_is_used_only_when_real_setting_is_missing(self):
        pet_core.SETTING.unlink()
        example = pet_core.SETTING.with_name("setting.example.md")
        example.write_text(share.SETTING_EXAMPLE, encoding="utf-8")
        pet_core.ensure_files()
        self.assertEqual(pet_core.SETTING.read_bytes(), example.read_bytes())
        pet_core.SETTING.write_text("private customization fixture", encoding="utf-8")
        pet_core.ensure_files()
        self.assertEqual(pet_core.SETTING.read_text(encoding="utf-8"), "private customization fixture")


if __name__ == "__main__":
    unittest.main()
