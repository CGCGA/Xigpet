import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from xigpet import app
from xigpet import pet_core
import test_pet_core as core_tests
from xigpet.daily_plan import DailyPlan, contact_period, contact_slots, GENERATED_MARKER
from xigpet.qq_bridge import QQSettings


def clock(hour=7, minute=0):
    return datetime(2026, 10, 22, hour, minute, tzinfo=timezone(timedelta(hours=8)))


class DailyFrequencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plan = DailyPlan(Path(self.temp.name))

    def test_full_day_has_two_contacts_per_period_even_if_model_returns_empty(self):
        now = clock()
        self.plan.save_generated(now, [])
        rows = self.plan.read(now.date().isoformat())
        self.assertEqual(Counter(contact_period(row["time"]) for row in rows),
                         {"上午": 2, "下午": 2, "晚上": 2})
        minutes = [int(row["time"][:2]) * 60 + int(row["time"][3:]) for row in rows]
        self.assertTrue(all(after - before >= 30 for before, after in zip(minutes, minutes[1:])))
        self.assertTrue(self.plan.has_generated(now.date().isoformat()))
        self.assertEqual(len(list(self.plan.folder.glob("*.md"))), 1)

    def test_late_start_only_plans_available_periods_and_never_crowds_to_six(self):
        for hour, minute, expected in ((13, 0, {"下午": 2, "晚上": 2}),
                                       (17, 50, {"下午": 1, "晚上": 2}),
                                       (22, 50, {"晚上": 1}), (23, 0, {})):
            with self.subTest(hour=hour, minute=minute):
                other = DailyPlan(self.plan.folder / f"{hour}-{minute}")
                now = clock(hour, minute)
                other.save_generated(now, [])
                rows = other.read(now.date().isoformat())
                self.assertEqual(Counter(contact_period(row["time"]) for row in rows), expected)
                self.assertTrue(all(now.replace(hour=int(row["time"][:2]), minute=int(row["time"][3:]))
                                    >= now + timedelta(minutes=2) for row in rows))

    def test_model_middle_of_short_period_cannot_block_two_feasible_contacts(self):
        now = clock(11, 18)
        self.plan.save_generated(now, [{"time": "11:40", "topic": "不能挤掉第二次联系"}])
        morning = [row for row in self.plan.read(now.date().isoformat()) if contact_period(row["time"]) == "上午"]
        self.assertEqual(len(morning), 2)

    def test_existing_reminders_sent_status_and_generated_marker_survive_edits(self):
        now = clock()
        self.plan.create(now, [{"time": "09:00", "topic": "用户约定"}])
        self.plan.mark_sent(now.date().isoformat(), "09:00", "用户约定")
        self.assertFalse(self.plan.has_generated(now.date().isoformat()))
        self.plan.save_generated(now, [{"time": "14:20", "topic": "模型挑选的话题"}])
        rows = self.plan.read(now.date().isoformat())
        self.assertEqual(len(rows), 6)
        self.assertEqual(next(row for row in rows if row["time"] == "09:00")["status"], "x")
        self.assertIn("模型挑选的话题", [row["topic"] for row in rows])
        self.plan.remove(now, "14:20")
        before = self.plan.current_path.read_text(encoding="utf-8")
        self.plan.save_generated(now, [])
        self.assertEqual(self.plan.current_path.read_text(encoding="utf-8"), before)
        self.assertTrue(self.plan.has_generated(now.date().isoformat()))

    def test_generated_contacts_catch_up_once_within_current_period(self):
        now = clock()
        events = [{"time": time, "topic": time} for time in ("08:30", "10:00", "13:00", "16:00", "19:00", "21:00")]
        self.plan.save_generated(now, events)
        due = self.plan.due(clock(11))
        self.assertEqual(due["time"], "10:00")
        self.assertEqual(self.plan.read(now.date().isoformat())[0]["status"], "~")
        self.plan.mark_sent(now.date().isoformat(), due["time"], due["topic"], sent_at=clock(11))
        self.assertIsNone(self.plan.due(clock(11, 1)))
        self.assertEqual(self.plan.due(clock(13))["time"], "13:00")

    def test_automatic_contacts_respect_actual_delivery_cooldown(self):
        now = clock()
        self.plan.save_generated(now, [{"time": "10:00", "topic": "第一条"}, {"time": "11:15", "topic": "第二条"}])
        due = self.plan.due(clock(11, 10))
        self.plan.mark_sent(now.date().isoformat(), due["time"], due["topic"], sent_at=clock(11, 10))
        self.assertIsNone(self.plan.due(clock(11, 15)))
        self.assertEqual(self.plan.due(clock(11, 41))["time"], "11:15")

    def test_old_period_is_not_dumped_on_resume_and_manual_reminders_keep_expiry(self):
        now = clock()
        self.plan.save_generated(now, [{"time": "08:30", "topic": "早上"}, {"time": "10:00", "topic": "上午"}])
        self.assertIsNone(self.plan.due(clock(12, 1)))
        morning = [row for row in self.plan.read(now.date().isoformat()) if contact_period(row["time"]) == "上午"]
        self.assertTrue(all(row["status"] == "~" for row in morning))
        manual = DailyPlan(self.plan.folder / "manual")
        manual.create(clock(), [{"time": "09:00", "topic": "明确约定的提醒"}])
        self.assertIsNone(manual.due(clock(10)))
        self.assertEqual(manual.read(now.date().isoformat())[0]["status"], "~")

    def test_next_day_reuses_file_and_resets_generation_marker(self):
        now = clock()
        self.plan.save_generated(now, [])
        self.assertFalse(self.plan.has_generated((now + timedelta(days=1)).date().isoformat()))
        self.plan.save_generated(now + timedelta(days=1), [])
        self.assertEqual(len(self.plan.read((now + timedelta(days=1)).date().isoformat())), 6)
        self.assertFalse(self.plan.has_day(now.date().isoformat()))
        self.assertEqual(len(list(self.plan.folder.glob("*.md"))), 1)


class PlannerIntegrationTests(unittest.TestCase):
    setUp = core_tests.PetCoreTests.setUp

    def test_sparse_existing_today_plan_still_calls_model_once_and_fills_six(self):
        now = clock()
        pet_core.daily_plan().create(now, [{"time": "09:00", "topic": "保留用户提醒"}])
        response = {"tool_calls": [{"function": {"name": "write_daily_plan", "arguments": json.dumps({"events": []})}}]}
        with patch.object(pet_core, "datetime") as current, patch.object(pet_core, "call_deepseek", return_value=response) as api:
            current.now.return_value = now
            self.assertTrue(pet_core.plan_today())
            self.assertFalse(pet_core.plan_today())
        api.assert_called_once()
        self.assertEqual(len(pet_core.daily_plan().read(now.date().isoformat())), 6)
        self.assertIn("保留用户提醒", [row["topic"] for row in pet_core.daily_plan().read(now.date().isoformat())])

    def test_generation_failure_retries_without_marking_day_complete(self):
        now = clock()
        with patch.object(pet_core, "datetime") as current, patch.object(pet_core, "call_deepseek", side_effect=pet_core.PetError("offline")):
            current.now.return_value = now
            with self.assertRaises(pet_core.PetError):
                pet_core.plan_today()
        self.assertFalse(pet_core.daily_plan().has_generated(now.date().isoformat()))

    def test_proactive_partial_response_is_marked_sent_to_avoid_duplicates(self):
        window = app.PetWindow()
        self.addCleanup(window.destroy)
        plan = MagicMock()
        def partial(_topic, on_reply):
            on_reply("先发出的主动联系。")
            raise pet_core.PetError("后续气泡失败")
        with patch.object(pet_core, "daily_plan", return_value=plan), \
             patch.object(pet_core, "proactive_reply", side_effect=partial):
            window._proactive_worker("2026-10-22", {"time": "09:00", "topic": "问候"})
        plan.mark_sent.assert_called_once()
        self.assertEqual(window.events.get_nowait()[0], "proactive_reply")
        self.assertEqual(window.events.get_nowait()[0], "proactive_error")

    def test_settings_import_remembers_external_snowluma_folder(self):
        window = app.PetWindow()
        self.addCleanup(window.destroy)
        config = pet_core.KEY_FILE.parent / "qq_bridge.json"
        moved_folder = Path(self.temp.name) / "external-drive" / "SnowLuma" / "config"
        moved_folder.mkdir(parents=True)
        saved = QQSettings(target_qq="20002", snowluma_config=str(moved_folder / "onebot_10001.json"))
        saved.save(config)
        window._open_qq_settings()
        with patch("xigpet.qq_settings.filedialog.askopenfilename", return_value="") as choose:
            window._qq_window._import()
        self.assertEqual(choose.call_args.kwargs["initialdir"], str(moved_folder))


if __name__ == "__main__":
    unittest.main()
