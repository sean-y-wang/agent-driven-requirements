"""Deterministic validation & normalization of LLM output (design §6). The LLM is never trusted."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from ..config.models import FieldSpec, Option


class Missing(Exception):
    """Value absent or could not be normalized; triggers a follow-up question."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Context:
    sender_open_id: str
    now: datetime
    tz: str = "Asia/Shanghai"
    sender_name: str = ""
    chat_name: str = ""
    message_link: str = ""
    raw_text: str = ""
    context_text: str = ""
    mentions: dict[str, str] = field(default_factory=dict)  # name -> open_id
    # resolves a display name to candidate open_ids among chat members
    member_lookup: Callable[[str], list[str]] | None = None


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s).strip().casefold()


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def match_option(raw: str, options: list[Option]) -> str | None:
    raw_s = str(raw).strip()
    for o in options:  # exact
        if raw_s == o.value:
            return o.value
    for o in options:  # synonyms
        if raw_s in o.synonyms:
            return o.value
    key = nfkc(raw_s)
    for o in options:  # case / full-width
        if key == nfkc(o.value) or key in (nfkc(s) for s in o.synonyms):
            return o.value
    hits = {
        o.value
        for o in options
        for cand in [o.value, *o.synonyms]
        if len(nfkc(cand)) > 1 and edit_distance(key, nfkc(cand)) <= 1
    }
    return hits.pop() if len(hits) == 1 else None


_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn_int(s: str) -> int:
    if not s:
        return 0
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                total += (section + num) * unit
                section = 0
            else:
                section += (num or 1) * unit
            num = 0
        else:
            raise ValueError(ch)
    return total + section + num


def parse_cn_number(s: str) -> float:
    neg = s.startswith(("负", "-"))
    s = s.lstrip("负-")
    if "点" in s:
        ip, fp = s.split("点", 1)
        val = float(f"{_cn_int(ip)}.{''.join(str(_CN_DIGITS[c]) for c in fp)}")
    else:
        val = float(_cn_int(s))
    return -val if neg else val


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_CN_NUM_RE = re.compile(r"[负零〇一二两三四五六七八九十百千万点]+")
_HALF_RE = re.compile(r"(\d+|[零〇一二两三四五六七八九十百千]+)个?半")


def _number(v: Any, spec: FieldSpec) -> float | int:
    if isinstance(v, bool):
        raise Missing("not a number")
    if isinstance(v, (int, float)):
        n = float(v)
    else:
        s = unicodedata.normalize("NFKC", str(v)).replace(",", "").strip()
        if m := _HALF_RE.search(s):  # "3个半" / "两个半小时"
            g = m.group(1)
            n = (float(g) if g[-1].isdigit() else parse_cn_number(g)) + 0.5
        elif m := _NUM_RE.search(s):
            n = float(m.group())
        elif m := _CN_NUM_RE.search(s):
            try:
                n = parse_cn_number(m.group())
            except (ValueError, KeyError):
                raise Missing("not a number") from None
        else:
            raise Missing("not a number")
    if spec.min is not None and n < spec.min:
        raise Missing(f"小于最小值 {spec.min:g}")
    if spec.max is not None and n > spec.max:
        raise Missing(f"大于最大值 {spec.max:g}")
    if spec.precision is not None:
        n = round(n, spec.precision)
    return int(n) if n.is_integer() and not spec.precision else n


def _date(v: Any, ctx: Context) -> int:
    tz = ZoneInfo(ctx.tz)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v if v > 1e11 else v * 1000)
    s = str(v).strip()
    dt: datetime | None = None
    m = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", s)
    if m:
        dt = datetime.combine(date(*map(int, m.groups())), time(), tz)
    else:
        import dateparser

        parsed = dateparser.parse(
            s,
            languages=["zh", "en"],
            settings={
                "TIMEZONE": ctx.tz,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "RELATIVE_BASE": ctx.now.astimezone(tz).replace(tzinfo=None),
                "PREFER_DATES_FROM": "current_period",
            },
        )
        if parsed is None:
            raise Missing("无法识别的日期")
        dt = datetime.combine(parsed.astimezone(tz).date(), time(), tz)
    return int(dt.timestamp() * 1000)


def _user(v: Any, ctx: Context) -> list[dict[str, str]]:
    items = v if isinstance(v, list) else [v]
    out: list[dict[str, str]] = []
    for it in items:
        if isinstance(it, dict):
            it = it.get("id") or it.get("open_id") or it.get("name") or ""
        s = str(it).strip().lstrip("@")
        if not s:
            continue
        if s.startswith("ou_"):
            out.append({"id": s})
            continue
        if s in ctx.mentions:
            out.append({"id": ctx.mentions[s]})
            continue
        if s == ctx.sender_name and ctx.sender_open_id:
            out.append({"id": ctx.sender_open_id})
            continue
        cands = ctx.member_lookup(s) if ctx.member_lookup else []
        if len(cands) == 1:
            out.append({"id": cands[0]})
        elif len(cands) > 1:
            raise Missing(f"“{s}”匹配到多位群成员，请 @ 具体的人")
        else:
            raise Missing(f"找不到成员“{s}”，请 @ 对方")
    if not out:
        raise Missing()
    return out


def _url(v: Any) -> dict[str, str]:
    if isinstance(v, dict):
        v = v.get("link") or v.get("url") or ""
    s = str(v).strip()
    if s and "://" not in s and re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", s, re.I):
        s = "https://" + s
    p = urlparse(s)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise Missing("链接格式不正确")
    return {"text": s, "link": s}


def _checkbox(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = nfkc(str(v))
    if s in ("true", "yes", "y", "1", "是", "对", "已", "完成", "✓"):
        return True
    if s in ("false", "no", "n", "0", "否", "不", "未", "没有"):
        return False
    raise Missing("无法识别是/否")


def is_empty(v: Any) -> bool:
    return v is None or (isinstance(v, (str, list, dict)) and len(v) == 0)


def normalize_value(spec: FieldSpec, v: Any, ctx: Context) -> Any:
    """Return Bitable write value or raise Missing."""
    if is_empty(v):
        raise Missing()
    t = spec.type
    if t in ("text", "phone"):
        s = str(v).strip() if not isinstance(v, list) else "\n".join(map(str, v)).strip()
        if not s:
            raise Missing()
        if t == "phone":
            s = re.sub(r"[\s-]", "", unicodedata.normalize("NFKC", s))
            if not re.fullmatch(r"\+?\d{5,20}", s):
                raise Missing("电话号码格式不正确")
        if spec.max_len:
            s = s[: spec.max_len]
        return s
    if t == "number":
        return _number(v, spec)
    if t == "single_select":
        hit = match_option(v[0] if isinstance(v, list) and v else v, spec.options)
        if hit is None:
            raise Missing(f"可选值：{'/'.join(o.value for o in spec.options)}")
        return hit
    if t == "multi_select":
        items = v if isinstance(v, list) else re.split(r"[,，、/;；\s]+", str(v))
        out: list[str] = []
        for it in items:
            if is_empty(it):
                continue
            hit = match_option(it, spec.options)
            if hit is None:
                raise Missing(f"“{it}”不在可选值中：{'/'.join(o.value for o in spec.options)}")
            if hit not in out:
                out.append(hit)
        if not out:
            raise Missing()
        return out
    if t == "date":
        return _date(v, ctx)
    if t == "checkbox":
        return _checkbox(v)
    if t == "user":
        return _user(v, ctx)
    if t == "url":
        return _url(v)
    if t == "attachment":
        items = v if isinstance(v, list) else [v]
        out = [{"file_token": it["file_token"]} for it in items if isinstance(it, dict) and it.get("file_token")]
        if not out:
            raise Missing()
        return out
    raise Missing(f"unsupported type {t}")  # pragma: no cover


# Variables that are always filled by the system (never asked from / overridable by the LLM).
AUTO_VARS = frozenset({"$raw_text", "$context", "$chat_name", "$message_link", "$now", "$sender"})


def is_auto(spec: FieldSpec) -> bool:
    return isinstance(spec.default, str) and spec.default in AUTO_VARS


def resolve_default(spec: FieldSpec, ctx: Context) -> Any:
    d = spec.default
    if d is None:
        return None
    local_now = ctx.now.astimezone(ZoneInfo(ctx.tz))
    if d == "$sender":
        return ctx.sender_open_id or None
    if d == "$sender_name":
        if spec.type == "user":
            return ctx.sender_open_id or None
        return ctx.sender_name or ctx.sender_open_id or None
    if d == "$today":
        return local_now.date().isoformat()
    if d == "$now":
        if spec.type == "date":
            return int(ctx.now.timestamp() * 1000)
        return local_now.strftime("%Y-%m-%d %H:%M:%S")
    if d == "$chat_name":
        return ctx.chat_name or None
    if d == "$message_link":
        return ctx.message_link or None
    if d == "$raw_text":
        return ctx.raw_text or None
    if d == "$context":
        return ctx.context_text or None
    return d


@dataclass
class ValidationResult:
    values: dict[str, Any]  # key -> bitable value
    missing: dict[str, str]  # key -> reason (may be "")

    @property
    def complete(self) -> bool:
        return not self.missing


def validate(fields: list[FieldSpec], raw: dict[str, Any], ctx: Context) -> ValidationResult:
    """Normalize raw extracted values; fill defaults afterwards; report missing required fields."""
    values: dict[str, Any] = {}
    missing: dict[str, str] = {}
    for spec in fields:
        reason = ""
        if is_auto(spec):
            d = resolve_default(spec, ctx)
            if d is not None:
                try:
                    values[spec.key] = normalize_value(spec, d, ctx)
                    continue
                except Missing as e:
                    reason = e.reason
            if spec.required:
                missing[spec.key] = reason or "系统未能获取"
            continue
        try:
            values[spec.key] = normalize_value(spec, raw.get(spec.key), ctx)
            continue
        except Missing as e:
            reason = e.reason
        d = resolve_default(spec, ctx)
        if d is not None and not reason:
            try:
                values[spec.key] = normalize_value(spec, d, ctx)
                continue
            except Missing as e:
                reason = e.reason
        if spec.required:
            missing[spec.key] = reason
        # optional fields with invalid values are dropped rather than written
    return ValidationResult(values, missing)
