"""Build JSON schema + prompt from a role template (design §6)."""

from __future__ import annotations

from typing import Any

from ..config.models import FieldSpec, Role
from ..validator.normalize import is_auto

_TYPE_SCHEMA: dict[str, dict[str, Any]] = {
    "text": {"type": ["string", "null"]},
    "phone": {"type": ["string", "null"]},
    "url": {"type": ["string", "null"]},
    "number": {"type": ["number", "string", "null"]},
    "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
    "checkbox": {"type": ["boolean", "null"]},
    "single_select": {"type": ["string", "null"]},
    "multi_select": {"type": ["array", "null"], "items": {"type": "string"}},
    "user": {"type": ["array", "null"], "items": {"type": "string"}, "description": "open_id(ou_...) 或姓名"},
}


def field_schema(spec: FieldSpec) -> dict[str, Any]:
    s = dict(_TYPE_SCHEMA[spec.type])
    if spec.type == "single_select":
        s["enum"] = [o.value for o in spec.options] + [None]
    if spec.type == "multi_select":
        s["items"] = {"type": "string", "enum": [o.value for o in spec.options]}
    return s


def llm_fields(role: Role) -> list[FieldSpec]:
    """Fields the LLM may fill (system-filled traceability fields are excluded)."""
    return [f for f in role.fields if not is_auto(f) and f.llm and f.type not in ("attachment", "link")]


def build_schema(role: Role) -> dict[str, Any]:
    fields = llm_fields(role)
    keys = [f.key for f in fields]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["fields", "missing", "confidence"],
        "properties": {
            "fields": {
                "type": "object",
                "additionalProperties": False,
                "required": keys,
                "properties": {f.key: field_schema(f) for f in fields},
            },
            "missing": {"type": "array", "items": {"type": "string", "enum": keys}},
            "confidence": {
                "type": "object",
                "additionalProperties": False,
                "required": keys,
                "properties": {k: {"type": ["number", "null"]} for k in keys},
            },
        },
    }


def describe_fields(role: Role) -> str:
    lines = []
    for f in llm_fields(role):
        parts = [f"- {f.key}（{f.field}）类型={f.type}", "必填" if f.required else "可选"]
        if f.description:
            parts.append(f"说明：{f.description}")
        if f.options:
            opts = "；".join(o.value + (f"（同义：{'、'.join(o.synonyms)}）" if o.synonyms else "") for o in f.options)
            parts.append(f"可选值：{opts}")
        if f.type == "number" and (f.min is not None or f.max is not None):
            parts.append(f"范围：{f.min if f.min is not None else '-∞'}~{f.max if f.max is not None else '∞'}")
        lines.append("，".join(parts))
    return "\n".join(lines)
