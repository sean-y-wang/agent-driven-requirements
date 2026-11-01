"""Convert lark-oapi event objects into IncomingMessage."""

from __future__ import annotations

from typing import Any

from ..router import IncomingMessage, Mention


def _g(obj: Any, *path: str, default: Any = "") -> Any:
    for p in path:
        if obj is None:
            return default
        obj = obj.get(p) if isinstance(obj, dict) else getattr(obj, p, None)
    return default if obj is None else obj


def to_incoming(data: Any) -> IncomingMessage:
    """Accepts lark_oapi P2ImMessageReceiveV1 or the equivalent dict payload."""
    msg = _g(data, "event", "message", default=None)
    mentions = [
        Mention(key=_g(m, "key"), name=_g(m, "name"), open_id=_g(m, "id", "open_id"))
        for m in (_g(msg, "mentions", default=[]) or [])
    ]
    return IncomingMessage(
        event_id=_g(data, "header", "event_id"),
        message_id=_g(msg, "message_id"),
        chat_id=_g(msg, "chat_id"),
        chat_type=_g(msg, "chat_type") or "group",
        sender_open_id=_g(data, "event", "sender", "sender_id", "open_id"),
        message_type=_g(msg, "message_type"),
        content=_g(msg, "content"),
        mentions=mentions,
        root_id=_g(msg, "root_id"),
        parent_id=_g(msg, "parent_id"),
        thread_id=_g(msg, "thread_id"),
        sender_type=_g(data, "event", "sender", "sender_type", default="user"),
        create_time_ms=int(_g(msg, "create_time") or 0),
    )
