"""Shared types and time handling for ChatPrint (Python standard library only)."""
from __future__ import annotations

import math
import re
from datetime import datetime, time, timezone, timedelta
from typing import TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MESSAGE_TYPES = ("text", "file", "sticker", "voice", "image", "video", "link", "other")
FORM_TYPES = ("text", "file", "sticker", "voice")
EMOTIONS = ("positive", "negative", "neutral", "mixed", "unknown")
TYPE_LABELS = {
    "text": "文字", "file": "文件", "sticker": "表情包", "voice": "语音",
    "image": "图片", "video": "视频", "link": "链接", "other": "其他",
}
EMOTION_LABELS = {
    "positive": "正面", "negative": "负面", "neutral": "中性", "mixed": "混合", "unknown": "无法判断",
}
DEFAULT_TZ = timezone(timedelta(hours=8))
SYSTEM_SENDER_ID = "__system__"


class Message(TypedDict):
    id: str
    chat_id: str
    chat_name: str
    sender_id: str
    sender_name: str
    timestamp: str
    type: str
    text: str


def resolve_timezone(value: str | None):
    """Return an explicit timezone; never use the host's current timezone."""
    if not value:
        return DEFAULT_TZ
    if value in ("UTC", "Z"):
        return timezone.utc
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", str(value))
    if match:
        hours, minutes = int(match[2]), int(match[3])
        if hours > 23 or minutes > 59:
            raise ValueError("时区偏移无效")
        seconds = (hours * 60 + minutes) * 60 * (1 if match[1] == "+" else -1)
        return timezone(timedelta(seconds=seconds))
    try:
        return ZoneInfo(str(value))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"无法识别时区：{value}") from exc


def parse_timestamp(value, default_tz=DEFAULT_TZ) -> tuple[str, bool]:
    """Normalize timestamps to +08:00. Return (ISO, assumed_timezone).

    Unix seconds, milliseconds, microseconds and nanoseconds are supported.
    A naive date/time means the explicitly configured timezone, +08:00 by default.
    Missing timestamps are allowed; invalid timestamps raise ValueError.
    """
    if value is None or str(value).strip() == "":
        return "", False
    if isinstance(value, bool):
        raise ValueError("时间不能为布尔值")
    assumed = False
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"-?\d+(?:\.\d+)?", str(value).strip()):
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError("时间不是有限数值")
            magnitude = abs(numeric)
            if magnitude >= 1e17:
                numeric /= 1e9
            elif magnitude >= 1e14:
                numeric /= 1e6
            elif magnitude >= 1e11:
                numeric /= 1e3
            dt = datetime.fromtimestamp(numeric, timezone.utc)
        else:
            raw = str(value).strip()
            if raw.endswith("Z") or raw.endswith("z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=default_tz)
                assumed = True
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"时间格式无效：{str(value)[:80]}") from exc
    return dt.astimezone(DEFAULT_TZ).isoformat(), assumed


def filter_boundary(value, end: bool = False) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    normalized, _ = parse_timestamp(raw)
    dt = datetime.fromisoformat(normalized)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        dt = datetime.combine(dt.date(), time.max if end else time.min, DEFAULT_TZ)
    return dt
