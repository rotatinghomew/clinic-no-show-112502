"""时间处理工具。资料中的时间均为无时区 ISO 字符串，按本地业务时间解释。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def parse_ts(value: Any) -> datetime | None:
    """解析 ISO8601 字符串；None 与空值保持为 None，绝不静默填充。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def week_start(value: datetime) -> datetime:
    """返回所在周的周一 00:00，用于按周期对比提醒策略。"""
    return datetime(value.year, value.month, value.day) - timedelta(days=value.weekday())


def floor_to_cycle(value: datetime, cycle_days: int) -> datetime:
    """以资料中最早时间为锚点划分固定长度周期。"""
    anchor = datetime(value.year, value.month, value.day)
    # 周一对齐，再按 cycle_days 取整，保证周期稳定可复现。
    anchor -= timedelta(days=value.weekday())
    delta = (value - anchor).days
    return anchor + timedelta(days=(delta // cycle_days) * cycle_days)


def parse_hhmm(value: str) -> tuple[int, int]:
    hour_str, minute_str = str(value).split(":", 1)
    return int(hour_str), int(minute_str)
