"""One reusable, human-editable proactive chat plan file."""

from __future__ import annotations

import os
import re
import tempfile
import threading
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path


ROW = re.compile(r"^- \[([ xX~-])\] ([0-2]\d:[0-5]\d) \| (.+)$")
PLAN_POLICY = "morning-afternoon-evening-2-v1"
GENERATED_MARKER = f"<!-- daily-planner: {PLAN_POLICY} -->"
CONTACT_PERIODS = (("上午", 8, 12), ("下午", 12, 18), ("晚上", 18, 23))
MIN_CONTACT_GAP = timedelta(minutes=30)
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _locked(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return run


def contact_period(value):
    hour = value.hour if isinstance(value, datetime) else int(value[:2])
    return next((name for name, start, end in CONTACT_PERIODS if start <= hour < end), None)


def _ceil_minute(value):
    rounded = value.replace(second=0, microsecond=0)
    return rounded + timedelta(minutes=1) if rounded < value else rounded


def contact_slots(now):
    """Suggested future slots, reduced near the end of a period rather than crowded."""
    slots = []
    earliest = _ceil_minute(now + timedelta(minutes=2))
    for name, start, end in CONTACT_PERIODS:
        left = max(earliest, now.replace(hour=start, minute=0, second=0, microsecond=0))
        right = now.replace(hour=end, minute=0, second=0, microsecond=0) - timedelta(minutes=1)
        if left > right:
            continue
        span = right - left
        if span >= MIN_CONTACT_GAP:
            inset = min(span / 3, (span - MIN_CONTACT_GAP) / 2)
            moments = (left + inset, right - inset)
        else:
            moments = (left + span / 2,)
        slots.extend({"period": name, "time": moment.strftime("%H:%M")} for moment in moments)
    return slots


def _replace(path: Path, content: str) -> None:
    fd, temp = tempfile.mkstemp(prefix="plan-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class DailyPlan:
    def __init__(self, folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.current_path = self.folder / "plan.md"
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.current_path.resolve()), threading.RLock())
        with self._lock:
            self._migrate_legacy_files()

    def _migrate_legacy_files(self) -> None:
        legacy = [path for path in self.folder.glob("????-??-??.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", path.name) and path.is_file()]
        if not legacy:
            return
        today_name = datetime.now().astimezone().date().isoformat() + ".md"
        today = self.folder / today_name
        if today in legacy:
            current_is_today = False
            if self.current_path.exists():
                with self.current_path.open("r", encoding="utf-8") as stream:
                    current_is_today = stream.readline().strip() == f"# {today.stem} 主动聊天计划"
            if not current_is_today:
                today.replace(self.current_path)
                legacy.remove(today)
        archive = self.folder / "archive"
        archive.mkdir(exist_ok=True)
        for path in legacy:
            destination = archive / path.name
            if not destination.exists():
                path.rename(destination)

    def path(self, day: str) -> Path:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("无效日期")
        return self.current_path

    @_locked
    def has_day(self, day: str) -> bool:
        path = self.path(day)
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8") as stream:
            return stream.readline().strip() == f"# {day} 主动聊天计划"

    @_locked
    def has_generated(self, day: str) -> bool:
        return self.has_day(day) and GENERATED_MARKER in self.current_path.read_text(encoding="utf-8").splitlines()

    def _metadata(self, day):
        if not self.has_day(day):
            return set(), None
        text = self.current_path.read_text(encoding="utf-8")
        match = re.search(r"<!-- automatic-times: ([\d:,]*) -->", text)
        automatic = set(match[1].split(",")) if match and match[1] else set()
        match = re.search(r"<!-- last-automatic-contact: ([^\n]+) -->", text)
        try:
            last = datetime.fromisoformat(match[1]) if match else None
        except ValueError:
            last = None
        return automatic, last

    @_locked
    def read(self, day: str) -> list[dict]:
        path = self.path(day)
        if not self.has_day(day):
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            match = ROW.fullmatch(line)
            if match and match[2][:2] < "24":
                rows.append({"status": match[1], "time": match[2], "topic": match[3]})
        return rows

    def _write(self, day: str, rows: list[dict], *, generated=None, automatic=None, last_contact=None) -> Path:
        path = self.path(day)
        previous_automatic, previous_contact = self._metadata(day)
        automatic = previous_automatic if automatic is None else automatic
        generated = self.has_generated(day) if generated is None else generated
        last_contact = previous_contact if last_contact is None else last_contact
        body = [f"# {day} 主动聊天计划", "", "时间为电脑本地时间；[ ] 待发送，[x] 已发送，[~] 已过期。", ""]
        if generated:
            body.extend([GENERATED_MARKER, "上午 08:00–12:00、下午 12:00–18:00、晚上 18:00–23:00，目标每段 2 次；较晚启动只安排剩余时间。"])
        automatic = automatic.intersection(row["time"] for row in rows)
        if automatic:
            body.append("<!-- automatic-times: " + ",".join(sorted(automatic)) + " -->")
        if last_contact is not None:
            body.append(f"<!-- last-automatic-contact: {last_contact.isoformat(timespec='seconds')} -->")
        body.append("")
        body.extend(f"- [{row['status']}] {row['time']} | {row['topic']}" for row in sorted(rows, key=lambda row: row["time"]))
        if not rows:
            body.append("今天暂无主动聊天安排。")
        _replace(path, "\n".join(body) + "\n")
        return path

    @staticmethod
    def _validate(time: str, topic: str, now: datetime) -> tuple[str, str]:
        if not isinstance(time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time):
            raise ValueError("时间须为今天的 HH:MM")
        if not isinstance(topic, str):
            raise ValueError("主题须为文本")
        topic = " ".join(topic.replace("|", "｜").split())
        if not topic or len(topic) > 160:
            raise ValueError("主题须为 1 至 160 字")
        scheduled = now.replace(hour=int(time[:2]), minute=int(time[3:]), second=0, microsecond=0)
        if scheduled < now + timedelta(minutes=2):
            raise ValueError("只能安排至少两分钟后的时间")
        return time, topic

    @_locked
    def create(self, now: datetime, events: list[dict]) -> Path:
        day = now.date().isoformat()
        path = self.path(day)
        if self.has_day(day):
            return path
        if not isinstance(events, list) or len(events) > 8:
            raise ValueError("当天最多安排 8 次主动聊天")
        rows = []
        seen = set()
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("计划项格式不正确")
            time, topic = self._validate(event.get("time"), event.get("topic"), now)
            if time in seen:
                raise ValueError("同一时间只能安排一项")
            seen.add(time)
            rows.append({"status": " ", "time": time, "topic": topic})
        return self._write(day, rows)

    @_locked
    def save_generated(self, now: datetime, events: list[dict]) -> Path:
        """Keep existing reminders/statuses and fill each available period to two."""
        day = now.date().isoformat()
        if self.has_generated(day):
            return self.path(day)
        if not isinstance(events, list):
            raise ValueError("计划 events 须为列表")
        rows = self.read(day)
        automatic, _ = self._metadata(day)
        slots = contact_slots(now)
        model_events = []
        for event in events[:8]:
            if not isinstance(event, dict):
                continue
            try:
                time, topic = self._validate(event.get("time"), event.get("topic"), now)
            except ValueError:
                continue
            model_events.append({"time": time, "topic": topic})
        fallback_topics = {
            "上午": ("结合人设和近期话题自然问候，聊聊今天想做的事情。", "关心上午过得如何，分享一件符合自己性格的小事。"),
            "下午": ("自然关心午后状态，结合记忆开启一个轻松话题。", "聊聊下午的见闻或趣事，关心用户是否需要稍作休息。"),
            "晚上": ("自然聊聊今天过得如何，结合用户作息关心晚饭或放松。", "分享今天的小心情，陪用户聊聊晚间安排；不强行催睡。"),
        }
        for period, start, end in CONTACT_PERIODS:
            future_slots = [slot for slot in slots if slot["period"] == period]
            if not future_slots:
                continue
            existing = sum(contact_period(row["time"]) == period and row["status"] not in ("~", "-") for row in rows)
            needed = min(2 - existing, len(future_slots), 8 - len(rows))
            if needed <= 0:
                continue
            # Reserve distinct times, at least 30 minutes apart, including adjacent periods.
            def available(time):
                moment = now.replace(hour=int(time[:2]), minute=int(time[3:]), second=0, microsecond=0)
                return all(abs(moment - now.replace(hour=int(row["time"][:2]), minute=int(row["time"][3:]), second=0, microsecond=0)) >= MIN_CONTACT_GAP
                           for row in rows if row["status"] not in ("~", "-"))
            candidates = [event for event in model_events if contact_period(event["time"]) == period]
            candidates += [{"time": slot["time"], "topic": fallback_topics[period][i % 2]}
                           for i, slot in enumerate(future_slots)]
            # A model-selected time can collide with a preferred fallback: use other free minutes.
            earliest = max(_ceil_minute(now + timedelta(minutes=2)), now.replace(hour=start, minute=0, second=0, microsecond=0))
            latest = now.replace(hour=end, minute=0, second=0, microsecond=0)
            cursor = earliest
            while cursor < latest:
                candidates.append({"time": cursor.strftime("%H:%M"), "topic": fallback_topics[period][len(candidates) % 2]})
                cursor += timedelta(minutes=5)
            for candidate in candidates:
                if needed <= 0:
                    break
                minute = int(candidate["time"][:2]) * 60 + int(candidate["time"][3:])
                room_for_next = needed == 1 or any(
                    abs(int(other["time"][:2]) * 60 + int(other["time"][3:]) - minute) >= 30
                    and available(other["time"]) for other in candidates)
                if available(candidate["time"]) and room_for_next:
                    rows.append({"status": " ", **candidate})
                    automatic.add(candidate["time"])
                    needed -= 1
        return self._write(day, rows, generated=True, automatic=automatic)

    @_locked
    def add(self, now: datetime, time: str, topic: str) -> Path:
        time, topic = self._validate(time, topic, now)
        day = now.date().isoformat()
        rows = self.read(day)
        if len(rows) >= 8:
            raise ValueError("当天最多安排 8 次主动聊天")
        if any(row["time"] == time for row in rows):
            raise ValueError("这个时间已有计划")
        rows.append({"status": " ", "time": time, "topic": topic})
        return self._write(day, rows)

    @_locked
    def remove(self, now: datetime, time: str) -> bool:
        day = now.date().isoformat()
        rows = self.read(day)
        remaining = [row for row in rows if row["time"] != time or row["status"] == "x"]
        if len(remaining) == len(rows):
            return False
        self._write(day, remaining)
        return True

    @_locked
    def due(self, now: datetime) -> dict | None:
        day = now.date().isoformat()
        rows = sorted(self.read(day), key=lambda row: row["time"])
        automatic, last_contact = self._metadata(day)
        changed = False
        manual_due = []
        automatic_due = []
        for row in rows:
            if row["status"] != " ":
                continue
            scheduled = now.replace(hour=int(row["time"][:2]), minute=int(row["time"][3:]), second=0, microsecond=0)
            delay = now - scheduled
            if row["time"] in automatic:
                if delay >= timedelta(0):
                    if contact_period(row["time"]) != contact_period(now):
                        row["status"] = "~"
                        changed = True
                    else:
                        automatic_due.append(row)
            elif delay > timedelta(minutes=15):
                row["status"] = "~"
                changed = True
            elif delay >= timedelta(0):
                manual_due.append(row)
        # Resume with at most the latest automatic message for this period, never a burst.
        if len(automatic_due) > 1:
            for row in automatic_due[:-1]:
                row["status"] = "~"
                changed = True
        if changed:
            self._write(day, rows)
        if manual_due:
            return manual_due[0]
        if automatic_due:
            if last_contact is not None:
                try:
                    if timedelta(0) <= now - last_contact < MIN_CONTACT_GAP:
                        return None
                except TypeError:
                    pass
            return automatic_due[-1]
        return None

    @_locked
    def mark_sent(self, day: str, time: str, topic: str, *, sent_at: datetime | None = None) -> bool:
        rows = self.read(day)
        for row in rows:
            if row["time"] == time and row["topic"] == topic and row["status"] == " ":
                row["status"] = "x"
                automatic, _ = self._metadata(day)
                last = sent_at if time in automatic else None
                self._write(day, rows, last_contact=last)
                return True
        return False
