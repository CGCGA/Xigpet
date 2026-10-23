import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
import zipfile

from scripts import updater, update_installer, build_release
from xigpet import app
import test_pet_core as core_tests


def digest(data):
    return hashlib.sha256(data).hexdigest()


class UpdatePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def package(self, *, source=True, files=None, extra=None, corrupt=False):
        files = files or {"main.py": b"new main", "VERSION.json": b'{"version":"1.2.0"}',
                          "config/setting.example.md": b"public template"}
        if not source:
            files["Xigpet.exe"] = b"fake executable for file tests only"
        hashes = {name: digest(data) for name, data in files.items()}
        if corrupt:
            hashes["main.py"] = "0" * 64
        manifest = ({name: {"sha256": value} for name, value in hashes.items()} if source else
                    {"format": 1, "version": "1.2.0", "kind": "windows-x64", "files": hashes})
        archive = self.root / ("source.zip" if source else "windows.zip")
        prefix = "Xigpet-commit/" if source else ""
        with zipfile.ZipFile(archive, "w") as package:
            for name, data in files.items():
                package.writestr(prefix + name, data)
            package.writestr(prefix + ("SHARE_MANIFEST.json" if source else "UPDATE_MANIFEST.json"), json.dumps(manifest))
            if extra:
                for name, data in extra.items():
                    package.writestr(prefix + name, data)
        info = updater.UpdateInfo("1.2.0", "https://github.com/CGCGA/Xigpet/releases/download/v1.2.0/test.zip",
                                  "source" if source else "windows-x64", digest(archive.read_bytes()))
        return archive, info

    def test_source_and_windows_packages_are_fully_verified_before_install(self):
        for source in (True, False):
            with self.subTest(source=source):
                archive, info = self.package(source=source)
                payload = self.root / str(source)
                files = updater.unpack_package(archive, payload, info)
                self.assertEqual((payload / "main.py").read_bytes(), b"new main")
                self.assertIn("VERSION.json", files)
                self.assertEqual("Xigpet.exe" in files, not source)

    def test_invalid_hash_unlisted_paths_private_files_and_symlinks_are_rejected(self):
        cases = [dict(corrupt=True), dict(extra={"../escape.py": b"bad"}),
                 dict(extra={"config/deepseek_key.txt": b"bad"}),
                 dict(files={"main.py": b"new", "VERSION.json": b'{"version":"1.2.0"}',
                             "memory/important.md": b"never overwrite"})]
        for index, arguments in enumerate(cases):
            with self.subTest(arguments=list(arguments)):
                archive, info = self.package(**arguments)
                with self.assertRaises(updater.UpdateError):
                    updater.unpack_package(archive, self.root / f"bad-{index}", info)
        archive, info = self.package()
        with zipfile.ZipFile(archive, "a") as package:
            link = zipfile.ZipInfo("Xigpet-commit/scripts/link.py")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(link, "../../private")
        with self.assertRaises(updater.UpdateError):
            updater.unpack_package(archive, self.root / "links", info)
        self.assertFalse((self.root / "escape.py").exists())

    def test_download_digest_and_cancellation_do_not_touch_live_files(self):
        archive, info = self.package(source=False)
        live = self.root / "app"
        live.mkdir()
        (live / "main.py").write_bytes(b"old main")
        def download(_url, target, _progress, _cancel):
            shutil.copyfile(archive, target)
        with patch.object(updater, "_download", side_effect=download):
            prepared = updater.prepare_update(live, info)
            self.assertTrue((prepared.stage / "payload/main.py").exists())
            bad = updater.UpdateInfo(info.version, info.url, info.kind, "0" * 64)
            with self.assertRaises(updater.UpdateError):
                updater.prepare_update(live, bad)
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(updater.UpdateCancelled):
                updater.prepare_update(live, info, cancel=cancelled)
        self.assertEqual((live / "main.py").read_bytes(), b"old main")

    def test_transaction_preserves_private_data_and_rolls_back_a_failed_copy(self):
        archive, info = self.package()
        payload = self.root / "payload"
        files = updater.unpack_package(archive, payload, info)
        live = self.root / "live"
        live.mkdir()
        (live / "main.py").write_bytes(b"old main")
        (live / "VERSION.json").write_bytes(b'{"version":"1.1.0"}')
        private = ("config/deepseek_key.txt", "config/setting.md", "config/qq_bridge.json",
                   "memory/chat_history.jsonl", "memory/images/screen.png", "memory/notes/todo.md",
                   "plans/plan.md", "media/drawings/mine.png")
        for name in private:
            target = live / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"private fixture bytes")
        original_replace = update_installer.os.replace
        attempts = 0
        def fail_second(source, target):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise PermissionError("fixture locked file")
            return original_replace(source, target)
        with patch.object(update_installer.os, "replace", side_effect=fail_second):
            with self.assertRaises(PermissionError):
                update_installer.apply_files(live, payload, files, self.root / "backup-failed")
        self.assertEqual((live / "main.py").read_bytes(), b"old main")
        self.assertEqual(json.loads((live / "VERSION.json").read_bytes())["version"], "1.1.0")
        self.assertFalse((live / "config/setting.example.md").exists())
        update_installer.apply_files(live, payload, files, self.root / "backup-ok")
        self.assertEqual((live / "main.py").read_bytes(), b"new main")
        for name in private:
            self.assertEqual((live / name).read_bytes(), b"private fixture bytes")

    def test_installer_rejects_private_file_even_with_valid_digest(self):
        payload = self.root / "payload"
        (payload / "config").mkdir(parents=True)
        (payload / "config/deepseek_key.txt").write_bytes(b"bad")
        with self.assertRaises(ValueError):
            update_installer.apply_files(self.root / "live", payload, {"config/deepseek_key.txt": digest(b"bad")}, self.root / "backup")
        self.assertFalse((self.root / "backup").exists())

    def test_installer_retries_a_brief_windows_file_lock(self):
        live, payload = self.root / "live-retry", self.root / "payload-retry"
        live.mkdir()
        payload.mkdir()
        (live / "main.py").write_bytes(b"old")
        (payload / "main.py").write_bytes(b"new")
        original = update_installer.os.replace
        calls = 0
        def briefly_locked(source, target):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("fixture bootloader lock")
            return original(source, target)
        with patch.object(update_installer.os, "replace", side_effect=briefly_locked), \
             patch.object(update_installer.time, "sleep"):
            update_installer.apply_files(live, payload, {"main.py": digest(b"new")}, self.root / "retry-backup", retry_seconds=1)
        self.assertEqual(calls, 2)
        self.assertEqual((live / "main.py").read_bytes(), b"new")

    def test_stock_media_index_merges_and_preserves_user_tags(self):
        live, payload = self.root / "live-index", self.root / "payload-index"
        (live / "media").mkdir(parents=True)
        (payload / "media").mkdir(parents=True)
        existing = {"my-picture.png": {"title": "private custom label"}, "happy.png": {"mood": "my mood"}}
        incoming = {"new.png": {"title": "new built-in"}, "happy.png": {"mood": "default"}}
        (live / "media/index.json").write_text(json.dumps(existing))
        data = json.dumps(incoming).encode()
        (payload / "media/index.json").write_bytes(data)
        update_installer.apply_files(live, payload, {"media/index.json": digest(data)}, self.root / "index-backup")
        merged = json.loads((live / "media/index.json").read_text(encoding="utf-8"))
        self.assertEqual(merged["my-picture.png"], existing["my-picture.png"])
        self.assertEqual(merged["happy.png"], existing["happy.png"])
        self.assertEqual(merged["new.png"], incoming["new.png"])

    def test_shared_program_allowlist_matches_updater_allowlist(self):
        from scripts.prepare_share import TOP_FILES, MEDIA
        names = [*TOP_FILES, *("media/" + name for name in MEDIA), "media/index.json",
                 "config/setting.example.md", "config/deepseek_key.example.txt", "config/qq_bridge.example.json",
                 ".gitignore", ".gitattributes", "README.md", "scripts/updater.py", "xigpet/app.py"]
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(update_installer.allowed_file(name))

    def test_installer_launch_is_hidden_and_source_helper_is_staged(self):
        live = self.root / "app"
        stage = live / ".updates/update-fixture"
        stage.mkdir(parents=True)
        prepared = updater.PreparedUpdate(stage, "1.2.0", {}, False)
        with patch.object(updater.subprocess, "Popen") as process:
            updater.launch_installer(live, prepared)
        self.assertTrue((stage / "update_installer.py").is_file())
        plan = json.loads((stage / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(plan["root"]), live)
        self.assertEqual(process.call_args.kwargs["creationflags"], getattr(updater.subprocess, "CREATE_NO_WINDOW", 0))

    def test_release_builder_produces_a_compatible_checked_windows_archive(self):
        root = self.root / "project"
        root.mkdir()
        (root / "VERSION.json").write_text('{"version":"1.2.0"}')
        exe = root / "Xigpet.exe"
        exe.write_bytes(b"not executed fixture")
        def public(_root, clean):
            clean.mkdir()
            contents = {"VERSION.json": b'{"version":"1.2.0"}', "main.py": b"public main"}
            for name, data in contents.items():
                (clean / name).write_bytes(data)
            (clean / "SHARE_MANIFEST.json").write_text(json.dumps({name: {"sha256": digest(data)} for name, data in contents.items()}))
        with patch.object(build_release, "executable_version", return_value="1.2.0"), \
             patch.object(build_release, "prepare_share", side_effect=public):
            archive = build_release.build_release(root, exe, root / "dist")
        self.assertTrue(archive.with_suffix(".zip.sha256").read_text().startswith(digest(archive.read_bytes())))
        files = updater.unpack_package(archive, self.root / "release-payload", updater.UpdateInfo("1.2.0", "", "windows-x64"))
        self.assertIn("Xigpet.exe", files)

    def test_real_source_helper_waits_for_process_exit_installs_and_restarts(self):
        live = self.root / "helper-app"
        stage = live / ".updates/update-helper-test"
        payload = stage / "payload"
        payload.mkdir(parents=True)
        (live / "main.py").write_bytes(b"old main")
        main_code = b"from pathlib import Path\nPath(__file__).with_name('restarted.txt').write_text('new version')\n"
        files = {"main.py": digest(main_code), "VERSION.json": digest(b'{"version":"1.2.0"}')}
        (payload / "main.py").write_bytes(main_code)
        (payload / "VERSION.json").write_bytes(b'{"version":"1.2.0"}')
        helper = stage / "update_installer.py"
        shutil.copyfile(Path(update_installer.__file__), helper)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"], creationflags=flags)
        plan = {"root": str(live), "pid": parent.pid, "version": "1.2.0", "files": files,
                "frozen": False, "python": sys.executable, "exe_name": "Xigpet.exe"}
        (stage / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        installer = subprocess.Popen([sys.executable, "-B", str(helper), "--plan", str(stage / "plan.json")], creationflags=flags)
        try:
            self.assertEqual(parent.wait(timeout=5), 0)
            self.assertEqual(installer.wait(timeout=10), 0)
            deadline = time.monotonic() + 3
            while not (live / "restarted.txt").exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertEqual((live / "restarted.txt").read_text(), "new version")
            self.assertTrue(json.loads((live / ".updates/result.json").read_text())["success"])
            self.assertEqual((stage / "backup/main.py").read_bytes(), b"old main")
        finally:
            for process in (parent, installer):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)


class UpdateDiscoveryTests(unittest.TestCase):
    def test_numeric_versions_and_pinned_source_commit(self):
        self.assertGreater(updater.version_tuple("1.10.0"), updater.version_tuple("v1.9.9"))
        with patch.object(updater, "_json", side_effect=[{"sha": "a" * 40}, {"version": "1.2.0"}]):
            info = updater.check_for_update("1.1.0", frozen=False)
        self.assertEqual(info.kind, "source")
        self.assertTrue(info.url.endswith("a" * 40))
        with patch.object(updater, "_json", side_effect=[{"sha": "a" * 40}, {"version": "1.1.0"}]):
            self.assertIsNone(updater.check_for_update("1.1.0", frozen=False))

    def test_windows_discovery_requires_a_release_asset_and_checksum(self):
        release = {"tag_name": "v1.2.0", "assets": [{"name": "Xigpet-v1.2.0-windows-x64.zip",
            "browser_download_url": "https://github.com/CGCGA/Xigpet/releases/download/v1.2.0/Xigpet-v1.2.0-windows-x64.zip",
            "digest": "sha256:" + "a" * 64}]}
        with patch.object(updater, "_json", return_value=release):
            self.assertEqual(updater.check_for_update("1.1.0", frozen=True).digest, "a" * 64)
        release["assets"][0]["digest"] = None
        with patch.object(updater, "_json", return_value=release), self.assertRaises(updater.UpdateError):
            updater.check_for_update("1.1.0", frozen=True)
        with patch.object(updater, "_json", side_effect=HTTPError("", 404, "not found", {}, None)), self.assertRaisesRegex(updater.UpdateError, "尚未发布 Windows 更新包"):
            updater.check_for_update("1.1.0", frozen=True)
        with patch.object(updater, "_json", side_effect=HTTPError("", 404, "not found", {}, None)), self.assertRaisesRegex(updater.UpdateError, "源码版本"):
            updater.check_for_update("1.1.0", frozen=False)


class UpdateWindowTests(unittest.TestCase):
    setUp = core_tests.PetCoreTests.setUp

    def window(self):
        with patch.object(app.PetWindow, "_schedule_tick"):
            window = app.PetWindow()
        self.addCleanup(window.destroy)
        with patch.object(app.threading, "Thread"):
            window._check_updates()
        return window

    def test_check_events_do_not_turn_into_chat_and_old_cancelled_checks_are_ignored(self):
        window = self.window()
        before = list(window.messages)
        old = window._update_cancel
        window.events.put(("update_finished", (old, "已是最新版本")))
        window._drain_events()
        self.assertFalse(window._update_busy)
        self.assertEqual(window.messages, before)
        window._cancel_update()
        with patch.object(app.threading, "Thread"):
            window._check_updates()
        window._handle_update_event("update_ready", old, MagicMock())
        self.assertIsNone(window._prepared_update)

    def test_ready_update_waits_for_chat_then_installs_without_touching_private_files(self):
        window = self.window()
        prepared = MagicMock(version="1.2.0")
        window.busy = True
        window._handle_update_event("update_ready", window._update_cancel, prepared)
        with patch.object(updater, "launch_installer") as launch:
            self.assertFalse(window._maybe_apply_update())
            launch.assert_not_called()
            window.busy = False
            self.assertTrue(window._maybe_apply_update())
            launch.assert_called_once()
        self.assertTrue(window._closing)

    def test_failed_installer_keeps_window_running(self):
        window = self.window()
        window._prepared_update = MagicMock(version="1.2.0")
        with patch.object(updater, "launch_installer", side_effect=OSError("fixture failure")):
            self.assertFalse(window._maybe_apply_update())
        self.assertFalse(window._closing)
        self.assertFalse(window._update_busy)
        self.assertIsNone(window._prepared_update)


if __name__ == "__main__":
    unittest.main()
