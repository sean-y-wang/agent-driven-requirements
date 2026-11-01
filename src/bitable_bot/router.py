"""Message parsing & command routing (design §3 router)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum

from .config.models import AppConfig, Role

HELP_COMMANDS = ("/help", "/帮助")
CANCEL_COMMANDS = ("/取消", "/cancel")


@dataclass
class Mention:
    key: str  # placeholder in text, e.g. "@_user_1"
    name: str
    open_id: str


@dataclass
class IncomingMessage:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str  # "group" | "p2p"
    sender_open_id: str
    message_type: str
    content: str  # raw JSON content string
    mentions: list[Mention] = field(default_factory=list)
    root_id: str = ""
    parent_id: str = ""
    thread_id: str = ""
    create_time_ms: int = 0
    sender_type: str = "user"


def extract_text(msg_type: str, content: str) -> str:
    try:
        data = json.loads(content or "{}")
    except json.JSONDecodeError:
        return ""
    if msg_type == "text":
        return str(data.get("text", ""))
    if msg_type == "post":  # rich text: flatten text/at nodes
        body = data if "content" in data else next(iter(data.values()), {}) if data else {}
        parts: list[str] = []
        if body.get("title"):
            parts.append(body["title"])
        for line in body.get("content", []):
            seg = []
            for node in line:
                if node.get("tag") in ("text", "a"):
                    seg.append(node.get("text", ""))
                elif node.get("tag") == "at":
                    seg.append(node.get("user_name") or f"@{node.get('user_id', '')}")
            parts.append("".join(seg))
        return "\n".join(parts)
    return ""


def bot_mentioned(mentions: list[Mention], text: str, bot_open_id: str | None) -> bool:
    if bot_open_id:
        return any(m.open_id == bot_open_id for m in mentions)
    # unknown bot id: fall back to "message starts with a mention" (or any mention for non-text messages)
    if not text.strip():
        return bool(mentions)
    return any(text.strip().startswith(m.key) for m in mentions)


def clean_text(
    text: str, mentions: list[Mention], bot_open_id: str | None, strip_leading: bool = True
) -> tuple[str, dict[str, str]]:
    """Remove the bot mention(s); render other mentions as @name. Returns (text, {name: open_id})."""
    people: dict[str, str] = {}
    text = text.strip()
    # the leading mention is treated as the bot when bot_open_id is unknown
    bot_found = False
    for m in sorted(mentions, key=lambda x: text.find(x.key) if x.key in text else 1 << 30):
        if bot_open_id:
            is_bot = m.open_id == bot_open_id
        else:
            is_bot = strip_leading and not bot_found and text.startswith(m.key)
        if is_bot:
            bot_found = True
            text = re.sub(re.escape(m.key) + r"(?!\d)", "", text, count=1).strip()
        else:
            people[m.name] = m.open_id
            text = re.sub(re.escape(m.key) + r"(?!\d)", f"@{m.name}", text)
    return re.sub(r"[ \t]+", " ", text).strip(), people


class Kind(Enum):
    HELP = "help"
    CANCEL = "cancel"
    ROLE = "role"
    UNKNOWN = "unknown"
    PLAIN = "plain"


@dataclass
class Route:
    kind: Kind
    body: str
    role: Role | None = None
    command: str = ""


def route(text: str, cfg: AppConfig) -> Route:
    t = text.strip()
    if not t.startswith(("/", "／")):
        return Route(Kind.PLAIN, t)
    t = "/" + t[1:]
    candidates = [(c, None) for c in (*HELP_COMMANDS, *CANCEL_COMMANDS)]
    candidates += [(c, r) for r in cfg.roles for c in r.commands]
    for cmd, role in sorted(candidates, key=lambda x: -len(x[0])):
        if t == cmd or t.startswith(cmd) and (t[len(cmd)].isspace() or not cmd[1:].isascii()):
            body = t[len(cmd) :].strip()
            if cmd in HELP_COMMANDS:
                return Route(Kind.HELP, body, command=cmd)
            if cmd in CANCEL_COMMANDS:
                return Route(Kind.CANCEL, body, command=cmd)
            return Route(Kind.ROLE, body, role=role, command=cmd)
    return Route(Kind.UNKNOWN, t, command=t.split()[0])


def help_text(cfg: AppConfig, chat_id: str | None = None) -> str:
    name = cfg.display_name()
    if cfg.dedicated and cfg.roles:
        r = cfg.roles[0]
        lines = [
            f"我是{name}，目前专注两件事：",
            "1）提需求 — 直接 @我用自然语言说明，我会在话题里用卡片请你确认 / 取消 / 修改后再写入",
            "2）查进展 — 问「我的需求进展如何 / XX 上线了吗」，按你的提问和提出人匹配，卡片最多回 2 条",
        ]
        if cfg.query_docs_hint:
            lines.append(f"   （{cfg.query_docs_hint}）")
        if cfg.tables:
            lines.append("0.9：自动候选进入待分诊并标记复核；群进展静默写入进展日志待复核，不找本人确认。")
        lines.append(f"也可用命令 {r.command} …；/取消 放弃未完成的记录；/help 查看本帮助。")
        return "\n".join(lines)
    lines = ["可用命令（@我 + 命令 + 内容）："]
    for r in cfg.roles:
        if chat_id and r.allowed_chats and chat_id not in r.allowed_chats:
            continue
        alias = f"（别名 {' '.join(r.aliases)}）" if r.aliases else ""
        req = "、".join(f.field for f in r.fields if f.required)
        lines.append(f"• {r.command}{alias} — {r.name}；必填：{req or '无'}")
    lines.append("• /取消 — 放弃当前未完成的记录")
    lines.append("• /help — 查看本帮助")
    lines.append(f"例：@我 {cfg.roles[0].command} 今天完成了登录模块，明天联调" if cfg.roles else "")
    return "\n".join(x for x in lines if x)
