"""§4.7 schedule mode: slot maths plus a JSON queue the CLI can drain later.

The queue file is the job record — a crash between queueing and posting loses nothing.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # stdlib since 3.9, but tzdata may be absent on Windows
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def tz_for(name: str):
    if ZoneInfo is None or not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def slots(
    schedule: dict,
    count: int,
    now: datetime,
    taken: list[datetime] | None = None,
    horizon_days: int = 30,
) -> list[datetime]:
    """The next `count` posting times for one account, honouring cadence and minimum gap.

    `taken` is what that account is already booked for — without it, every call would hand
    back the same next free slot and stack a day's posts on one timestamp.
    """
    tz = tz_for(schedule.get("timezone", "UTC"))
    now = now.astimezone(tz)
    per_day = max(1, int(schedule.get("per_platform_per_day", 1)))
    gap = timedelta(minutes=int(schedule.get("min_gap_minutes", 0)))
    times = sorted(schedule.get("times") or ["09:00"])
    booked = sorted(t.astimezone(tz) for t in (taken or []))

    chosen: list[datetime] = []
    for day in range(horizon_days):
        date = (now + timedelta(days=day)).date()
        posted_today = sum(1 for t in booked if t.date() == date)
        for clock in times:
            if posted_today >= per_day or len(chosen) >= count:
                break
            hour, minute = (int(part) for part in clock.split(":"))
            candidate = datetime(date.year, date.month, date.day, hour, minute, tzinfo=tz)
            if candidate <= now:
                continue
            if any(abs(candidate - other) < gap for other in booked + chosen):
                continue
            chosen.append(candidate)
            posted_today += 1
        if len(chosen) >= count:
            break
    return chosen


class Queue:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    def add(self, platform: str, account_id: str, asset_ref: str, metadata: dict, when: datetime) -> str:
        rows = self._read()
        queued_id = "q_%s" % uuid.uuid4().hex[:10]
        rows.append(
            {
                "queued_id": queued_id,
                "platform": platform,
                "account_id": account_id,
                "asset_ref": asset_ref,
                "metadata": metadata,
                "scheduled_for": when.isoformat(),
                "status": "queued",
            }
        )
        self._write(rows)
        return queued_id

    def booked(self, platform: str, account_id: str) -> list[datetime]:
        return [
            datetime.fromisoformat(row["scheduled_for"])
            for row in self._read()
            if row["status"] == "queued"
            and row["platform"] == platform
            and row["account_id"] == account_id
        ]

    def due(self, now: datetime) -> list[dict]:
        return [
            row
            for row in self._read()
            if row["status"] == "queued"
            and datetime.fromisoformat(row["scheduled_for"]) <= now.astimezone(timezone.utc)
        ]

    def mark(self, queued_id: str, status: str, detail: str | None = None) -> None:
        rows = self._read()
        for row in rows:
            if row["queued_id"] == queued_id:
                row["status"] = status
                row["detail"] = detail
        self._write(rows)
