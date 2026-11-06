"""时间处理：一律以 UTC ISO-8601 存储，展示时转本地偏移。

本地偏移用固定小时数（默认 +8），不依赖系统 tzdata——
Windows 上 zoneinfo 需要额外安装 tzdata 包，那会破坏"零依赖"。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def now_utc_iso() -> str:
    return to_iso_utc(datetime.now(UTC))


def local_str(iso: str | None, offset_hours: float = 8) -> str:
    dt = parse_iso(iso)
    if dt is None:
        return "未知时间"
    loc = dt.astimezone(timezone(timedelta(hours=offset_hours)))
    return loc.strftime("%Y-%m-%d %H:%M:%S")


def local_date_parts(iso: str | None, offset_hours: float = 8) -> tuple[str, str, str]:
    dt = parse_iso(iso)
    if dt is None:
        dt = datetime.now(UTC)
    loc = dt.astimezone(timezone(timedelta(hours=offset_hours)))
    return loc.strftime("%Y"), loc.strftime("%m"), loc.strftime("%d")


def local_stamp(iso: str | None, offset_hours: float = 8) -> tuple[str, str]:
    """返回 (YYYYMMDD-HHMM, YYYY/MM/DD)，用于附件目录与文件名。"""
    dt = parse_iso(iso)
    if dt is None:
        dt = datetime.now(UTC)
    loc = dt.astimezone(timezone(timedelta(hours=offset_hours)))
    return loc.strftime("%Y%m%d-%H%M"), loc.strftime("%Y/%m/%d")


def window_start_iso(hours: float, now: datetime | None = None) -> str:
    """N 小时窗口的起点（UTC ISO），用于热记忆的时间范围查询。"""
    now = now or datetime.now(UTC)
    return to_iso_utc(now - timedelta(hours=hours))


def age_days(iso: str | None, now: datetime | None = None) -> float | None:
    dt = parse_iso(iso)
    if dt is None:
        return None
    now = now or datetime.now(UTC)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def decay(age: float | None, half_life_days: float) -> float:
    """指数衰减：age 为 None（时间未知）时返回中性权重。"""
    if age is None or half_life_days <= 0:
        return 0.55
    return math.exp(-math.log(2) * age / half_life_days)
