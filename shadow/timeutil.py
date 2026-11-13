"""Time-zone helpers (phase 7).

Day boundaries (the 50-trade daily counter, the daily report, the daily backup) use one configured
time zone, PKT (Asia/Karachi, UTC+5, no DST) by default, independent of the phone's own TZ setting.
If the zoneinfo database is missing (possible on a bare Termux), PKT falls back to a fixed UTC+5.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone, tzinfo

FIXED = {"Asia/Karachi": timezone(timedelta(hours=5), "PKT"), "PKT": timezone(timedelta(hours=5), "PKT"),
         "UTC": timezone.utc}
_TZ: tzinfo = FIXED["Asia/Karachi"]
_NAME = "Asia/Karachi"


def resolve(name: str) -> tuple[tzinfo, str | None]:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), None
    except Exception:
        if name in FIXED:
            return FIXED[name], None
        return FIXED["Asia/Karachi"], f"TIMEZONE={name} unknown here, using PKT (UTC+5)"


def set_timezone(name: str) -> str | None:
    global _TZ, _NAME
    _TZ, warn = resolve(name or "Asia/Karachi")
    _NAME = name if not warn else "Asia/Karachi"
    return warn


def tz() -> tzinfo:
    return _TZ


def tz_name() -> str:
    return _NAME


def local_dt(ts: float, zone: tzinfo | None = None) -> datetime:
    return datetime.fromtimestamp(ts, zone or _TZ)


def day_start(ts: float | None = None, zone: tzinfo | None = None) -> float:
    """Unix ts of local midnight (configured TZ, PKT by default) of the day containing ts."""
    d = local_dt(time.time() if ts is None else ts, zone)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def day_key(ts: float | None = None, zone: tzinfo | None = None) -> str:
    return local_dt(time.time() if ts is None else ts, zone).strftime("%Y-%m-%d")


def fmt(ts: float | None, f: str = "%d %b %H:%M", zone: tzinfo | None = None) -> str:
    return local_dt(ts, zone).strftime(f) if ts else "-"


def iso(ts: float | None) -> str:
    return local_dt(ts).isoformat(timespec="seconds") if ts else ""


__all__ = ["set_timezone", "tz", "tz_name", "day_start", "day_key", "fmt", "iso", "local_dt", "resolve"]
