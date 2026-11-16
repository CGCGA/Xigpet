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
PLAN_POLICY = "mood-adaptive-drawing-2-v3"
GENERATED_MARKER = f"<!-- daily-planner: {PLAN_POLICY} -->"
CONTACT_PERIODS = (("上午", 8, 12), ("下午", 12, 18), ("晚上", 18, 23))
MIN_CONTACT_GAP = timedelta(minutes=30)
MAX_DRAWINGS = 2
DRAWING_PREFIX = "[自主画画] "
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def mood_budget(mood=75):
    """Daily initiative budget; reminders are independent of the pet's mood."""
    chats = 1 if mood < 40 else 2 if mood < 70 else 3 if mood < 80 else 4 if mood < 90 else 5
    drawings = 0 if mood < 40 else 1 if mood < 80 else 2
    return chats, drawings


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


def drawing_slots(now):
    """One optional creative slot per remaining daytime period."""
    earliest = _ceil_minute(now + timedelta(minutes=2))
    slots = []
    for period, start, end in CONTACT_PERIODS:
        left = max(earliest, now.replace(hour=start, minute=0, second=0, microsecond=0))
        right = now.replace(hour=end, minute=0, second=0, microsecond=0) - timedelta(minutes=1)
        if left <= right:
            slots.append({"kind": "drawing", "period": period,
                          "time": (left + (right - left) / 2).strftime("%H:%M")})
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
                topic = match[3]
                row = {"status": match[1], "time": match[2], "topic": topic}
                if topic.startswith(DRAWING_PREFIX):
                    row.update(kind="drawing", topic=topic[len(DRAWING_PREFIX):])
                rows.append(row)
        return rows

    def _drawing_attempts(self, day):
        if not self.has_day(day):
            return 0
        text = self.current_path.read_text(encoding="utf-8")
        match = re.search(r"<!-- drawing-attempts: (\d+) -->", text)
        recorded = int(match[1]) if match else 0
        started = sum(row.get("kind") == "drawing" and row["status"] in ("x", "-")
                      for row in self.read(day))
        return max(recorded, started)

    def _write(self, day: str, rows: list[dict], *, generated=None, automatic=None, last_contact=None, drawing_attempts=None, budget_marker=None) -> Path:
        path = self.path(day)
        if budget_marker is None and self.has_day(day):
            match = re.search(r"<!-- mood-budget: \d+,\d+ -->", path.read_text(encoding="utf-8"))
            budget_marker = match[0] if match else None
        previous_automatic, previous_contact = self._metadata(day)
        automatic = previous_automatic if automatic is None else automatic
        generated = self.has_generated(day) if generated is None else generated
        last_contact = previous_contact if last_contact is None else last_contact
        drawing_attempts = self._drawing_attempts(day) if drawing_attempts is None else drawing_attempts
        body = [f"# {day} 主动聊天计划", "", "时间为电脑本地时间；[ ] 待执行，[x] 已完成，[~] 已过期，[-] 绘画已开始（失败或重启后不自动重试）。", ""]
        if generated:
            body.extend([GENERATED_MARKER, "08:00–23:00 按心情安排：心情低于70时每天主动联系1–2次，否则3–5次；自主画画随心情减少，全天最多2次，也可以不画。较晚启动只安排剩余时间。"])
        if budget_marker:
            body.append(budget_marker)
        body.append(f"<!-- drawing-attempts: {drawing_attempts} -->")
        automatic = automatic.intersection(row["time"] for row in rows)
        if automatic:
            body.append("<!-- automatic-times: " + ",".join(sorted(automatic)) + " -->")
        if last_contact is not None:
            body.append(f"<!-- last-automatic-contact: {last_contact.isoformat(timespec='seconds')} -->")
        body.append("")
        body.extend(f"- [{row['status']}] {row['time']} | "
                    + (DRAWING_PREFIX if row.get("kind") == "drawing" else "") + row["topic"]
                    for row in sorted(rows, key=lambda row: row["time"]))
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
    def save_generated(self, now: datetime, events: list[dict], *, mood=75) -> Path:
        """Keep reminders and completed work; adapt future initiative to current mood."""
        day = now.date().isoformat()
        chat_budget, drawing_budget = mood_budget(mood)
        budget_marker = f"<!-- mood-budget: {chat_budget},{drawing_budget} -->"
        if self.has_generated(day) and budget_marker in self.current_path.read_text(encoding="utf-8").splitlines():
            return self.path(day)
        if not isinstance(events, list):
            raise ValueError("计划 events 须为列表")
        rows = self.read(day)
        automatic, _ = self._metadata(day)
        # Reuse creative decisions when adapting, without another model request.
        pending = [{**row, "kind": row.get("kind", "chat")} for row in rows
                   if row["status"] == " " and (row["time"] in automatic or row.get("kind") == "drawing")]
        rows = [row for row in rows
                if not (row["status"] == " " and (row["time"] in automatic or row.get("kind") == "drawing"))]
        events = events + pending
        automatic.intersection_update(row["time"] for row in rows)
        slots = contact_slots(now)
        # Spread the whole-day budget across periods. Late launches don't catch up.
        counts = {"上午": 0, "下午": 0, "晚上": 0}
        for period in ("上午", "晚上", "下午", "下午", "晚上")[:chat_budget]:
            counts[period] += 1
        spent = sum(row["time"] in automatic and row["status"] == "x" for row in rows)
        remaining = max(0, chat_budget - spent)
        model_events = []
        for event in events:
            if not isinstance(event, dict):
                continue
            try:
                time, topic = self._validate(event.get("time"), event.get("topic"), now)
            except ValueError:
                continue
            if event.get("kind", "chat") not in ("chat", "drawing"):
                continue
            model_events.append({"time": time, "topic": topic, "kind": event.get("kind", "chat")})
        fallback_topics = {
            "上午": ("结合人设和近期话题自然问候，聊聊今天想做的事情。", "关心上午过得如何，分享一件符合自己性格的小事。"),
            "下午": ("自然关心午后状态，结合记忆开启一个轻松话题。", "聊聊下午的见闻或趣事，关心用户是否需要稍作休息。"),
            "晚上": ("自然聊聊今天过得如何，结合用户作息关心晚饭或放松。", "分享今天的小心情，陪用户聊聊晚间安排；不强行催睡。"),
        }
        for period, start, end in CONTACT_PERIODS:
            future_slots = [slot for slot in slots if slot["period"] == period]
            if not future_slots:
                continue
            existing = sum(row["time"] in automatic and row.get("kind") != "drawing" and contact_period(row["time"]) == period
                           and row["status"] not in ("~", "-") for row in rows)
            chat_count = sum(row.get("kind") != "drawing" for row in rows)
            needed = min(counts[period] - existing, len(future_slots), 8 - chat_count, remaining)
            if needed <= 0:
                continue
            # Reserve distinct times, at least 30 minutes apart, including adjacent periods.
            def available(time):
                moment = now.replace(hour=int(time[:2]), minute=int(time[3:]), second=0, microsecond=0)
                return all(abs(moment - now.replace(hour=int(row["time"][:2]), minute=int(row["time"][3:]), second=0, microsecond=0)) >= MIN_CONTACT_GAP
                           for row in rows if row["status"] not in ("~", "-"))
            candidates = [event for event in model_events if event["kind"] == "chat"
                          and contact_period(event["time"]) == period]
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
                    remaining -= 1
        # Drawing is optional; both planning and execution enforce the daily cap.
        # Retain old rows and spent attempts when upgrading today's existing plan.
        drawing_count = max(self._drawing_attempts(day), sum(row.get("kind") == "drawing" for row in rows))
        for event in model_events:
            if event["kind"] != "drawing" or drawing_count >= drawing_budget:
                continue
            if contact_period(event["time"]) is None:
                continue
            moment = now.replace(hour=int(event["time"][:2]), minute=int(event["time"][3:]), second=0, microsecond=0)
            if any(abs(moment - now.replace(hour=int(row["time"][:2]), minute=int(row["time"][3:]), second=0, microsecond=0)) < MIN_CONTACT_GAP
                   for row in rows if row["status"] not in ("~", "-")):
                continue
            rows.append({"status": " ", **event})
            drawing_count += 1
        return self._write(day, rows, generated=True, automatic=automatic, budget_marker=budget_marker)

    @_locked
    def adapt_mood(self, now: datetime, mood: float) -> None:
        if self.has_generated(now.date().isoformat()):
            self.save_generated(now, [], mood=mood)

    @_locked
    def add(self, now: datetime, time: str, topic: str) -> Path:
        time, topic = self._validate(time, topic, now)
        day = now.date().isoformat()
        rows = self.read(day)
        if sum(row.get("kind") != "drawing" for row in rows) >= 8:
            raise ValueError("当天最多安排 8 次主动聊天")
        if any(row["time"] == time for row in rows):
            raise ValueError("这个时间已有计划")
        rows.append({"status": " ", "time": time, "topic": topic})
        return self._write(day, rows)

    @_locked
    def remove(self, now: datetime, time: str) -> bool:
        day = now.date().isoformat()
        rows = self.read(day)
        remaining = [row for row in rows if row["time"] != time or row["status"] == "x"
                     or (row.get("kind") == "drawing" and row["status"] == "-")]
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
            if row.get("kind") == "drawing":
                if delay > timedelta(minutes=15) or (delay >= timedelta(0) and self._drawing_attempts(day) >= MAX_DRAWINGS):
                    row["status"] = "~"
                    changed = True
                elif delay >= timedelta(0):
                    manual_due.append(row)
            elif row["time"] in automatic:
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
    def start_drawing(self, day: str, time: str, topic: str) -> bool:
        """Reserve a daily attempt on disk before calling any model or drawing tool."""
        attempts = self._drawing_attempts(day)
        if attempts >= MAX_DRAWINGS:
            return False
        rows = self.read(day)
        for row in rows:
            if (row.get("kind") == "drawing" and row["status"] == " "
                    and row["time"] == time and row["topic"] == topic):
                row["status"] = "-"
                self._write(day, rows, drawing_attempts=attempts + 1)
                return True
        return False

    @_locked
    def mark_sent(self, day: str, time: str, topic: str, *, sent_at: datetime | None = None) -> bool:
        rows = self.read(day)
        for row in rows:
            if (row["time"] == time and row["topic"] == topic
                    and (row["status"] == " " or (row.get("kind") == "drawing" and row["status"] == "-"))):
                row["status"] = "x"
                automatic, _ = self._metadata(day)
                last = sent_at if time in automatic else None
                self._write(day, rows, last_contact=last)
                return True
        return False
