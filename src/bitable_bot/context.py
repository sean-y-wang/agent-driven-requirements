"""Collect conversation context & traceability metadata when the bot is triggered.

Every remote call is best-effort: missing scopes or API errors only shrink the context, never fail the request.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import structlog

from .config.models import AppConfig
from .router import IncomingMessage, extract_text
from .writer.feishu_api import FeishuAPI, FeishuAPIError

log = structlog.get_logger(__name__)

_PLACEHOLDER = {
    "image": "[图片]",
    "file": "[文件]",
    "audio": "[语音]",
    "media": "[视频]",
    "sticker": "[表情]",
    "interactive": "[卡片]",
    "share_chat": "[群名片]",
    "share_user": "[个人名片]",
    "merge_forward": "[合并转发]",
}


@dataclass
class ConversationContext:
    sender_name: str = ""
    chat_name: str = ""
    message_link: str = ""
    excerpt: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> ConversationContext:
        d = d or {}
        return cls(**{k: str(d.get(k) or "") for k in ("sender_name", "chat_name", "message_link", "excerpt")})


def build_message_link(item: dict[str, Any], chat_id: str, host: str) -> str:
    """Prefer API-provided message_app_link; else assemble an AppLink from position; else open the chat."""
    link = str(item.get("message_app_link") or "").strip()
    if link:
        return link
    if not host:  # no verified AppLink host for this deployment: omit rather than guess
        return ""
    chat = item.get("chat_id") or chat_id
    thread_id, tpos, mpos = item.get("thread_id"), item.get("thread_message_position"), item.get("message_position")
    if thread_id and chat and tpos is not None:
        q = {
            "openthreadid": thread_id,
            "openchatid": chat,
            "open_thread_id": thread_id,
            "open_chat_id": chat,
            "thread_position": str(tpos),
        }
        return f"https://{host}/client/thread/open?{urlencode(q)}"
    if chat and mpos is not None:
        return f"https://{host}/client/chat/open?{urlencode({'openChatId': chat, 'position': str(mpos)})}"
    return f"https://{host}/client/chat/open?{urlencode({'openChatId': chat})}" if chat else ""


class ContextCollector:
    def __init__(
        self,
        api: FeishuAPI,
        cfg: AppConfig,
        applink_host: str = "applink.feishu.cn",
        bot_open_id: str | None = None,
        clock=time.time,
    ) -> None:
        self.api = api
        self.cfg = cfg
        self.host = applink_host
        self.bot_open_id = bot_open_id
        self.clock = clock
        self._names: dict[str, tuple[str, float]] = {}
        self._members: dict[str, tuple[dict[str, str], float]] = {}
        self._chats: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------ names
    def _ttl(self) -> float:
        return self.clock() + self.cfg.context.name_cache_seconds

    def chat_members(self, chat_id: str) -> dict[str, str]:
        """open_id -> name (cached)."""
        hit = self._members.get(chat_id)
        if hit and hit[1] > self.clock():
            return hit[0]
        try:
            members = {oid: name for name, oid in self.api.list_chat_members(chat_id)}
        except FeishuAPIError as e:
            log.info("context.members_unavailable", code=e.code)
            members = {}
        self._members[chat_id] = (members, self._ttl())
        return members

    def resolve_name(self, open_id: str, chat_id: str = "") -> str:
        """Contact directory → chat member name → open_id."""
        if not open_id:
            return ""
        hit = self._names.get(open_id)
        if hit and hit[1] > self.clock():
            return hit[0]
        name = ""
        try:
            name = self.api.get_user_name(open_id)
        except FeishuAPIError as e:
            log.info("context.contact_unavailable", code=e.code)
        if not name and chat_id:
            name = self.chat_members(chat_id).get(open_id, "")
        if name:
            self._names[open_id] = (name, self._ttl())
        return name or open_id

    def chat_name(self, msg: IncomingMessage) -> str:
        if msg.chat_type == "p2p":
            return "单聊"
        hit = self._chats.get(msg.chat_id)
        if hit and hit[1] > self.clock():
            return hit[0]
        try:
            name = self.api.get_chat_name(msg.chat_id)
        except FeishuAPIError as e:
            log.info("context.chat_unavailable", code=e.code)
            name = ""
        self._chats[msg.chat_id] = (name, self._ttl())
        return name or msg.chat_id

    # ------------------------------------------------------------ messages
    def _sender_label(self, item: dict[str, Any], chat_id: str) -> str:
        sender = item.get("sender") or {}
        sid = sender.get("id") or ""
        if sender.get("sender_type") == "app" or (self.bot_open_id and sid == self.bot_open_id):
            return "机器人"
        return self.resolve_name(sid, chat_id) if sid else "未知"

    def render(self, item: dict[str, Any], chat_id: str) -> str:
        if item.get("deleted"):
            return ""
        msg_type = item.get("msg_type") or item.get("message_type") or ""
        content = (item.get("body") or {}).get("content") or item.get("content") or ""
        text = extract_text(msg_type, content) if msg_type in ("text", "post") else _PLACEHOLDER.get(msg_type, "")
        for m in item.get("mentions") or []:
            key, name = m.get("key") or "", m.get("name") or ""
            if key:
                text = text.replace(key, f"@{name}")
        text = " ".join(text.split())
        if not text:
            return ""
        if len(text) > 500:
            text = text[:497] + "..."
        ts = ""
        try:
            ct = int(item.get("create_time") or 0)
            if ct:
                ts = datetime.fromtimestamp(ct / 1000, ZoneInfo(self.cfg.timezone)).strftime("[%m-%d %H:%M] ")
        except (TypeError, ValueError):
            pass
        return f"{ts}{self._sender_label(item, chat_id)}：{text}"

    def collect(self, msg: IncomingMessage) -> ConversationContext:
        cc = self.cfg.context
        ctx = ConversationContext(
            sender_name=self.resolve_name(msg.sender_open_id, msg.chat_id if msg.chat_type != "p2p" else ""),
            chat_name=self.chat_name(msg),
        )
        current: dict[str, Any] = {}
        try:
            current = self.api.get_message(msg.message_id)
        except FeishuAPIError as e:
            log.info("context.message_unavailable", code=e.code)
        ctx.message_link = build_message_link(current, msg.chat_id, self.host)
        if not cc.enabled:
            return ctx

        seen = {msg.message_id}
        sections: list[tuple[str, list[str]]] = []
        if cc.include_parent and msg.parent_id:
            try:
                parent = self.api.get_message(msg.parent_id)
                line = self.render(parent, msg.chat_id) if parent else ""
                if line:
                    sections.append(("被回复/引用的消息", [line]))
                    seen.add(msg.parent_id)
            except FeishuAPIError as e:
                log.info("context.parent_unavailable", code=e.code)
        thread_id = msg.thread_id or current.get("thread_id") or ""
        if cc.include_thread and thread_id and cc.thread_messages:
            try:
                items = self.api.list_messages("thread", thread_id, cc.thread_messages, newest_first=False)
                lines = [self.render(i, msg.chat_id) for i in items if i.get("message_id") not in seen]
                seen.update(i.get("message_id") for i in items)
                if any(lines):
                    sections.append(("所在话题", [x for x in lines if x]))
            except FeishuAPIError as e:
                log.info("context.thread_unavailable", code=e.code)
        if cc.recent_messages:
            try:
                items = self.api.list_messages("chat", msg.chat_id, cc.recent_messages + 1, newest_first=True)
                items = [i for i in items if i.get("message_id") not in seen][: cc.recent_messages]
                lines = [x for x in (self.render(i, msg.chat_id) for i in reversed(items)) if x]
                if lines:
                    sections.append(("最近的聊天消息", lines))
            except FeishuAPIError as e:
                log.info("context.history_unavailable", code=e.code)
        ctx.excerpt = _fit(sections, cc.max_chars)
        return ctx


def _fit(sections: list[tuple[str, list[str]]], max_chars: int) -> str:
    def join(secs):
        return "\n".join(f"【{title}】\n" + "\n".join(lines) for title, lines in secs if lines)

    secs = [(t, list(lines)) for t, lines in sections]
    text = join(secs)
    # drop oldest lines first, starting from the least important (last) section
    while len(text) > max_chars and any(len(lines) > 1 for _, lines in secs):
        for _t, lines in reversed(secs):
            if len(lines) > 1:
                lines.pop(0)
                break
        text = join(secs)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def dumps(ctx: ConversationContext) -> str:
    return json.dumps(ctx.to_dict(), ensure_ascii=False)
