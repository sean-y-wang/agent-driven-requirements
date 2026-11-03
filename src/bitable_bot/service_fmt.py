"""Shared field value formatting for receipts / previews."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config.models import FieldSpec


def fmt_value(spec: FieldSpec, v: Any, people: dict[str, str], tz: str) -> str:
    if spec.type == "date":
        if isinstance(v, int):
            return datetime.fromtimestamp(v / 1000, UTC).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")
        return str(v)
    if spec.type == "user":
        rev = {oid: n for n, oid in people.items()}
        return "、".join(rev.get(u["id"], u["id"]) for u in v)
    if spec.type == "multi_select":
        return "、".join(v)
    if spec.type == "url":
        return v["link"] if isinstance(v, dict) else str(v)
    if spec.type == "checkbox":
        return "是" if v else "否"
    if spec.type == "attachment":
        return f"{len(v) if isinstance(v, list) else 1} 个文件"
    s = " ".join(str(v).split())
    return s if len(s) <= 80 else s[:77] + "..."
