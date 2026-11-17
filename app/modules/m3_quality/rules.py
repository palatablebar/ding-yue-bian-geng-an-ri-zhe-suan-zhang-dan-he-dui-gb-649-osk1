"""字段归一与判定：金额、日期、生效时刻、时区、状态、变更类型。

全部是纯函数，入参出参都是普通数据，便于单测；原因码只在这里产生，不散在 handler。
归一不隐瞒事实：千分位、全角、三位小数都会留下 info 级标记，原值另存。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from ... import config

FULLWIDTH_TABLE = {ord("０") + offset: str(offset) for offset in range(10)} | {
    ord("．"): ".", ord("，"): ",", ord("－"): "-", ord("／"): "/",
    ord("＋"): "+", ord("："): ":", ord("（"): "(", ord("）"): ")",
}
OFFSET_RE = re.compile(r"^(.*?)([+-])(\d{2}):?(\d{2})$")
DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


@dataclass
class Parsed:
    value: object = None
    code: str | None = None
    flags: list[str] = field(default_factory=list)


@dataclass
class Instant:
    naive: dt.datetime
    offset: dt.tzinfo | None = None
    zone_name: str | None = None


def to_halfwidth(text: str) -> str:
    return (text or "").translate(FULLWIDTH_TABLE)


def _is_sentinel(text: str) -> bool:
    return (text or "").strip().lower() in config.SENTINELS


def normalize_money(text: str) -> Parsed:
    """月价归一：千分位、全角数字、货币符号、单位后缀。"""
    raw = (text or "").strip()
    if _is_sentinel(raw):
        return Parsed(None, "R_PRICE_MISSING")
    flags: list[str] = []
    if any(ch in raw for ch in "０１２３４５６７８９"):
        flags.append("R_PRICE_FULLWIDTH")
    clean = to_halfwidth(raw).replace(" ", "")
    if "," in clean:
        flags.append("R_PRICE_THOUSANDS")
        clean = clean.replace(",", "")
    for token in ("CNY", "RMB", "¥", "￥", "元"):
        clean = clean.replace(token, "")
    clean = clean.strip()
    if not re.fullmatch(r"\d+(\.\d+)?", clean):
        return Parsed(None, "R_PRICE_FORMAT", flags)
    try:
        value = Decimal(clean)
    except InvalidOperation:
        return Parsed(None, "R_PRICE_FORMAT", flags)
    if value < 0:
        return Parsed(None, "R_PRICE_FORMAT", flags)
    if value.as_tuple().exponent < -2:
        flags.append("R_PRICE_PRECISION")
    return Parsed(value, None, flags)


def normalize_date(text: str) -> Parsed:
    """日期归一：只认 YYYY-MM-DD / YYYY/M/D / YYYY.M.D，非法日期不猜。"""
    raw = (text or "").strip()
    if _is_sentinel(raw):
        return Parsed(None, None)
    clean = to_halfwidth(raw)
    match = DATE_RE.match(clean.replace("/", "-").replace(".", "-"))
    if not match:
        return Parsed(None, "R_DATE_FORMAT")
    try:
        value = dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return Parsed(None, "R_DATE_FORMAT")
    return Parsed(value)


def parse_instant(text: str) -> Parsed:
    """生效时刻：支持 Z / ±HH:MM / ±HHMM / IANA 时区名 / 仅日期。"""
    raw = (text or "").strip()
    if _is_sentinel(raw):
        return Parsed(None, "R_DATE_FORMAT")
    clean = to_halfwidth(raw).replace("T", " ").strip()
    offset: dt.tzinfo | None = None
    zone_name: str | None = None

    if clean.endswith(("Z", "z")):
        offset = dt.timezone.utc
        clean = clean[:-1].strip()
    match = OFFSET_RE.match(clean)
    if match and " " in clean and not clean.endswith("/"):
        body, sign, hours, minutes = match.groups()
        total = int(hours) * 60 + int(minutes)
        if sign == "-":
            total = -total
        if abs(total) > 14 * 60:
            return Parsed(None, "R_DATE_FORMAT")
        offset = dt.timezone(dt.timedelta(minutes=total))
        clean = body.strip()
    tokens = clean.split()
    if offset is None and len(tokens) >= 2 and "/" in tokens[-1]:
        zone_name = tokens[-1]
        clean = " ".join(tokens[:-1])

    normalized = clean.replace("/", "-").replace(".", "-")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = dt.datetime.strptime(normalized, pattern)
        except ValueError:
            continue
        return Parsed(Instant(naive=naive, offset=offset, zone_name=zone_name))
    return Parsed(None, "R_DATE_FORMAT")


def parse_zone(text: str) -> Parsed:
    """时区：IANA 名或固定偏移；空值回退展示时区。"""
    raw = (text or "").strip()
    if not raw or _is_sentinel(raw):
        return Parsed(None, None)
    clean = to_halfwidth(raw)
    if clean.upper() in {"UTC", "GMT", "Z"}:
        return Parsed(dt.timezone.utc)
    match = OFFSET_RE.match(clean)
    if match:
        _body, sign, hours, minutes = match.groups()
        total = int(hours) * 60 + int(minutes)
        if sign == "-":
            total = -total
        if abs(total) > 14 * 60:
            return Parsed(None, "R_TZ_UNKNOWN")
        return Parsed(dt.timezone(dt.timedelta(minutes=total)))
    try:
        return Parsed(ZoneInfo(clean))
    except Exception:  # noqa: BLE001 - 非法时区名统一进隔离
        return Parsed(None, "R_TZ_UNKNOWN")


def normalize_state(text: str) -> Parsed:
    raw = to_halfwidth((text or "").strip()).lower()
    if not raw:
        return Parsed(None, "R_STATE_UNKNOWN")
    alias = {
        "active": "active", "生效中": "active", "在用": "active", "正常": "active",
        "paused": "paused", "暂停": "paused", "已暂停": "paused", "挂起": "paused",
        "trial": "trial", "试用": "trial", "试用期": "trial", "试用中": "trial",
        "cancelled": "cancelled", "canceled": "cancelled", "取消": "cancelled",
        "已取消": "cancelled", "解约": "cancelled",
    }
    value = alias.get(raw)
    return Parsed(value) if value else Parsed(None, "R_STATE_UNKNOWN")


def normalize_kind(text: str) -> Parsed:
    raw = to_halfwidth((text or "").strip())
    value = config.CHANGE_KINDS.get(raw)
    if value is None and raw:
        value = config.CHANGE_KINDS.get(raw.lower())
    return Parsed(value) if value else Parsed(None, "R_CHANGE_KIND")


def normalize_cycle(text: str) -> Parsed:
    """计费周期：认月付/季付/半年付/年付，也认 1/3/6/12 个月。

    返回值为周期月数；空值与哨兵返回 ``None``（由调用方决定是旧文件还是缺列）。
    """
    raw = to_halfwidth((text or "").strip())
    if _is_sentinel(raw):
        return Parsed(None)
    clean = raw.replace(" ", "").lower()
    value = config.CYCLE_MONTHS.get(clean)
    if value is None and clean.endswith("个月"):
        head = clean[:-2]
        if head.isdigit():
            value = int(head)
    if value is None and clean.isdigit():
        value = int(clean)
    if value is None or value <= 0:
        return Parsed(None, "R_CYCLE_UNKNOWN")
    return Parsed(value)


def contract_amount(text: str) -> Parsed:
    """合同金额：与月价同一套归一规则，但原因码独立，便于运营区分缺价与缺合同额。"""
    parsed = normalize_money(text)
    if parsed.code == "R_PRICE_MISSING":
        return Parsed(None, "R_CONTRACT_MISSING", parsed.flags)
    if parsed.code == "R_PRICE_FORMAT":
        return Parsed(None, "R_CONTRACT_FORMAT", parsed.flags)
    return parsed


def change_price(kind: str, text: str) -> Parsed:
    """变更后的月价：暂停/取消一律计 0；恢复允许沿用旧价。"""
    if kind in {"pause", "cancel"}:
        return Parsed(Decimal("0"))
    parsed = normalize_money(text)
    if parsed.value is None and kind == "resume" and _is_sentinel(text):
        return Parsed(None)
    return parsed


def effective_parts(parsed: Instant, sub_zone: dt.tzinfo) -> dt.datetime:
    """按生效时刻自带的偏移/时区定位；都没有时用订阅时区。"""
    if parsed.offset is not None:
        return parsed.naive.replace(tzinfo=parsed.offset).astimezone(config.UTC)
    return parsed.naive.replace(tzinfo=None)
