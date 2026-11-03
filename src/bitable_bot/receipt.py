"""Success / preview / duplicate receipts: interactive card with structured-text fallback."""

from __future__ import annotations

from typing import Any

from .config.models import FieldSpec, Role
from .service_fmt import fmt_value
from .writer.bitable import record_url

_HIDDEN = frozenset({"$context", "$raw_text"})
# Trace / provenance defaults — omit from confirm card body (footer only).
_TRACE_DEFAULTS = frozenset({"$context", "$raw_text", "$message_link", "$chat_name", "$now"})
_TRACE_KEYS = frozenset({"source", "raw_text", "context", "message_link", "submitted_at"})
# System-set markers — keep off confirm/receipt cards (users don't need to act on them).
_SYSTEM_KEYS = frozenset({"review", "agent"})


def visible_specs(role: Role, values: dict[str, Any] | None = None) -> list[FieldSpec]:
    out = []
    for spec in role.fields:
        if isinstance(spec.default, str) and spec.default in _HIDDEN:
            continue
        if values is not None and spec.key not in values:
            continue
        out.append(spec)
    return out


def confirm_specs(role: Role, values: dict[str, Any] | None = None) -> list[FieldSpec]:
    """Decision fields only: title/desc/business + proposer; no long trace/system fields."""
    out = []
    for spec in role.fields:
        if spec.key in _TRACE_KEYS or spec.key in _SYSTEM_KEYS:
            continue
        if isinstance(spec.default, str) and spec.default in _TRACE_DEFAULTS:
            continue
        if values is not None and spec.key not in values:
            continue
        out.append(spec)
    return out


def receipt_specs(role: Role, values: dict[str, Any] | None = None) -> list[FieldSpec]:
    """Oral success receipt: same decision fields as confirm, no provenance dump."""
    return confirm_specs(role, values)


def _trace_footer(role: Role, values: dict[str, Any], people: dict[str, str], tz: str) -> str:
    """One-line provenance: 来源群 · 提交时间."""
    bits: list[str] = []
    for key in ("source", "submitted_at"):
        if key not in values:
            continue
        spec = role.field_by_key(key)
        if not spec:
            continue
        bits.append(fmt_value(spec, values[key], people, tz))
    return " · ".join(b for b in bits if b)


def preview_lines(role: Role, values: dict[str, Any], people: dict[str, str], tz: str) -> list[str]:
    lines = ["请确认这条需求："]
    for spec in confirm_specs(role, values):
        lines.append(f"• {spec.field}：{fmt_value(spec, values[spec.key], people, tz)}")
    footer = _trace_footer(role, values, people, tz)
    if footer:
        lines.append(f"来源：{footer}")
    lines.append("回复「确认」收录；点卡片也可改/取消。改字段可直接说，如「优先级改成 P0」。")
    return lines


def success_text(
    role: Role, values: dict[str, Any], people: dict[str, str], tz: str, url: str, duplicated: bool = False
) -> str:
    title = role.receipt_title or (
        "🔁 这条已经记过了" if duplicated else "✅ 需求已收录"
    )
    if duplicated and role.receipt_title:
        title = f"🔁 {role.receipt_title}"
    lines = [title]
    for spec in receipt_specs(role, values):
        lines.append(f"{spec.field}：{fmt_value(spec, values[spec.key], people, tz)}")
    if url:
        lines.append(f"打开记录：{url}")
    return "\n".join(lines)


def success_card(
    role: Role, values: dict[str, Any], people: dict[str, str], tz: str, url: str, duplicated: bool = False
) -> dict[str, Any]:
    title = role.receipt_title or ("这条已经记过了" if duplicated else "需求已收录")
    prefix = "🔁 " if duplicated else "✅ "
    fields = []
    for spec in receipt_specs(role, values):
        v = fmt_value(spec, values[spec.key], people, tz)
        fields.append({"is_short": True, "text": {"tag": "lark_md", "content": f"**{spec.field}**\n{v}"}})
    elements: list[dict[str, Any]] = []
    if fields:
        # Feishu cards prefer groups of up to 5 short fields
        for i in range(0, len(fields), 5):
            elements.append({"tag": "div", "fields": fields[i : i + 5]})
    if url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开记录"},
                        "type": "primary",
                        "url": url,
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": prefix + title},
            "template": "orange" if duplicated else "green",
        },
        "elements": elements or [{"tag": "div", "text": {"tag": "plain_text", "content": url or title}}],
    }


def followup_text(role: Role, missing: dict[str, str], optional_empty: list[FieldSpec]) -> str:
    lines = ["还差这几项才能收录，直接在这回复就行："]
    for spec in role.fields:
        if spec.key in missing:
            hint = missing[spec.key] or spec.description or ""
            lines.append(f"• {spec.field}" + (f"（{hint}）" if hint else ""))
    if optional_empty:
        names = "、".join(f.field for f in optional_empty[:6])
        lines.append(f"选填（{names}）可不填；不想补就回「跳过」。")
    lines.append("不想继续了回 /取消。")
    return "\n".join(lines)


def confirm_card(
    role: Role,
    values: dict[str, Any],
    people: dict[str, str],
    tz: str,
    *,
    chat_id: str,
    user_id: str,
    source_message_id: str,
) -> dict[str, Any]:
    """Interactive confirm / cancel / modify card posted inside the topic thread.

    Shows decision fields only (title/desc/business/proposer). Trace fields collapse
    to one footer line 「来源群 · 时间」— no raw message / long links in the body.
    """
    fields = []
    for spec in confirm_specs(role, values):
        v = fmt_value(spec, values[spec.key], people, tz)
        # Truncate long desc for card readability
        if spec.key == "desc" and isinstance(v, str) and len(v) > 120:
            v = v[:117] + "..."
        fields.append({"is_short": True, "text": {"tag": "lark_md", "content": f"**{spec.field}**\n{v}"}})
    elements: list[dict[str, Any]] = []
    if fields:
        for i in range(0, len(fields), 5):
            elements.append({"tag": "div", "fields": fields[i : i + 5]})
    footer = _trace_footer(role, values, people, tz)
    if footer:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"_来源：{footer}_"},
            }
        )
    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "确认后收录到需求池。点 **修改** 后直接回复要改的内容（如「优先级改成 P0」）。",
            },
        }
    )
    payload_base = {
        "chat_id": chat_id,
        "user_id": user_id,
        "role": role.command,
        "source": source_message_id,
        "kind": "confirm",
    }
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "确认"},
                    "type": "primary",
                    "value": {**payload_base, "action": "confirm"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "修改"},
                    "type": "default",
                    "value": {**payload_base, "action": "modify"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "取消"},
                    "type": "danger",
                    "value": {**payload_base, "action": "cancel"},
                },
            ],
        }
    )
    title = "请确认这条需求"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": elements,
    }


def query_card(
    role: Role,
    summary: str,
    rows: list[dict[str, Any]],
    *,
    docs_hint: str | None = None,
    base_url: str = "https://feishu.cn",
) -> dict[str, Any]:
    """Interactive consult result card (title/status/priority/proposer/due + optional open link)."""
    elements: list[dict[str, Any]] = []
    if summary:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": summary}})
    for i, row in enumerate(rows, 1):
        title = row.get("one_liner") or row.get("title") or "记录"
        fields = []
        for key in role.query.reply_fields:
            spec = role.field_by_key(key)
            if not spec:
                continue
            val = row.get(key)
            if val in (None, "", []):
                continue
            # skip repeating title line if already in one_liner header
            if key == "title" and row.get("one_liner"):
                fields.append({"is_short": True, "text": {"tag": "lark_md", "content": f"**{spec.field}**\n{val}"}})
                continue
            fields.append({"is_short": True, "text": {"tag": "lark_md", "content": f"**{spec.field}**\n{val}"}})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{i}. {title}**"}})
        if fields:
            for j in range(0, len(fields), 5):
                elements.append({"tag": "div", "fields": fields[j : j + 5]})
        rid = str(row.get("record_id") or "")
        if rid:
            url = record_url(role, rid, base_url)
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开记录"},
                            "type": "default",
                            "url": url,
                        }
                    ],
                }
            )
    if docs_hint:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"_{docs_hint}_"}})
    if not elements:
        elements = [{"tag": "div", "text": {"tag": "plain_text", "content": summary or "暂无结果"}}]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"「{role.name}」咨询结果"},
            "template": "wathet",
        },
        "elements": elements,
    }
