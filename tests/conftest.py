from __future__ import annotations

import json
from pathlib import Path

import pytest

from bitable_bot.config import load_config
from bitable_bot.extractor import Extractor
from bitable_bot.query import QueryService
from bitable_bot.router import IncomingMessage, Mention
from bitable_bot.service import BotService
from bitable_bot.session import Store
from bitable_bot.writer import BitableWriter, FeishuAPIError

ROOT = Path(__file__).resolve().parents[1]
NOW = 1792728000.0  # 2026-10-23 (Fri) 12:00:00 Asia/Shanghai


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeAPI:
    def __init__(self) -> None:
        self.records: list[dict] = []
        self.replies: list[dict] = []
        self.members: list[tuple[str, str]] = []
        self.create_errors: list[FeishuAPIError] = []
        self._n = 0
        self.users: dict[str, str] = {}  # open_id -> contact name
        self.chat_names: dict[str, str] = {}
        self.messages: dict[str, dict] = {}  # message_id -> raw item (get_message)
        self.history: dict[tuple[str, str], list[dict]] = {}  # (container_type, id) -> items, oldest first
        self.fail: set[str] = set()  # method names that raise FeishuAPIError
        self.calls: list[tuple] = []
        self.bitable_rows: list[dict] = []  # for search/list records
        self.cards_ok = False  # False → reply_card raises → text fallback
        self.reactions: list[tuple] = []

    def create_record(self, app_token, table_id, fields, client_token):
        if self.create_errors:
            raise self.create_errors.pop(0)
        self.records.append(dict(app_token=app_token, table_id=table_id, fields=fields, client_token=client_token))
        return f"rec{len(self.records)}"

    def reply_text(self, message_id, text, in_thread=False):
        self._n += 1
        mid = f"om_bot{self._n}"
        self.replies.append(dict(message_id=message_id, text=text, in_thread=in_thread, id=mid, type="text"))
        return mid

    def reply_card(self, message_id, card, in_thread=False):
        if not self.cards_ok:
            raise FeishuAPIError(230001, "card not enabled in fake")
        self._n += 1
        mid = f"om_bot{self._n}"
        self.replies.append(
            dict(
                message_id=message_id,
                card=card,
                in_thread=in_thread,
                id=mid,
                type="interactive",
                text=card.get("header", {}).get("title", {}).get("content", ""),
            )
        )
        return mid

    def send_card(self, receive_id, card, *, receive_id_type="open_id"):
        self._maybe_fail("send_card")
        if not self.cards_ok:
            raise FeishuAPIError(230001, "card not enabled in fake")
        self._n += 1
        mid = f"om_bot{self._n}"
        self.replies.append(
            dict(
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                card=card,
                id=mid,
                type="interactive",
                text=card.get("header", {}).get("title", {}).get("content", ""),
            )
        )
        self.calls.append(("send_card", receive_id, receive_id_type))
        return mid

    def send_text(self, receive_id, text, *, receive_id_type="open_id"):
        self._maybe_fail("send_text")
        self._n += 1
        mid = f"om_bot{self._n}"
        self.replies.append(
            dict(receive_id=receive_id, receive_id_type=receive_id_type, text=text, id=mid, type="text")
        )
        self.calls.append(("send_text", receive_id, receive_id_type))
        return mid

    def search_records(self, app_token, table_id, filter_info, page_size=20):
        self._maybe_fail("search_records")
        items = [
            r
            for r in self.bitable_rows
            if r.get("app_token", app_token) == app_token and r.get("table_id", table_id) == table_id
        ]
        # naive contains filter
        conds = (filter_info or {}).get("conditions") or []
        if not conds:
            return items[:page_size]
        hit = []
        for row in items:
            fields = row.get("fields") or {}
            for c in conds:
                name, vals = c.get("field_name"), c.get("value") or []
                cell = fields.get(name)
                text = cell if isinstance(cell, str) else str(cell or "")
                if any(v and v in text for v in vals):
                    hit.append(row)
                    break
        return hit[:page_size]

    def list_records(self, app_token, table_id, page_size=30):
        self._maybe_fail("list_records")
        items = [
            r
            for r in self.bitable_rows
            if r.get("app_token", app_token) == app_token and r.get("table_id", table_id) == table_id
        ]
        return items[:page_size]

    def _maybe_fail(self, name):
        self.calls.append((name,))
        if name in self.fail:
            raise FeishuAPIError(99991672, f"{name} denied")

    def list_chat_members(self, chat_id):
        self._maybe_fail("list_chat_members")
        return self.members

    def get_bot_open_id(self):
        self._maybe_fail("get_bot_open_id")
        return "ou_bot"

    def get_message(self, message_id):
        self._maybe_fail("get_message")
        return self.messages.get(message_id, {})

    def list_messages(self, container_type, container_id, page_size, newest_first=True):
        self._maybe_fail(f"list_messages:{container_type}")
        items = list(self.history.get((container_type, container_id), []))
        if newest_first:
            items.reverse()
        return items[:page_size]

    def get_user_name(self, open_id):
        self._maybe_fail("get_user_name")
        return self.users.get(open_id, "")

    def get_chat_name(self, chat_id):
        self._maybe_fail("get_chat_name")
        return self.chat_names.get(chat_id, "")

    def add_reaction(self, message_id, emoji_type="Get"):
        self._maybe_fail("add_reaction")
        self.reactions.append((message_id, emoji_type))
        self.calls.append(("add_reaction", message_id, emoji_type))


class FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self, outputs=None) -> None:
        self.outputs = list(outputs or [])
        self.calls: list[list[dict]] = []

    def extract(self, messages, schema):
        self.calls.append(messages)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


@pytest.fixture
def cfg():
    env = {"OPENAI_API_KEY": "k", "DASHSCOPE_API_KEY": "k", "DEEPSEEK_API_KEY": "k", "PRIVATE_LLM_KEY": "k"}
    c = load_config(ROOT / "config" / "roles.example.yaml", env=env)
    c.first_guide_enabled = False
    c.ack_after_seconds = 0
    return c


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def harness(cfg, clock):
    class H:
        pass

    h = H()
    h.api = FakeAPI()
    h.store = Store(":memory:", clock=clock)
    h.provider = FakeProvider()
    providers = {name: h.provider for name in cfg.llm.providers}
    providers_map = providers
    extractor = Extractor(providers_map, cfg.llm.default)
    qsvc = QueryService(h.api, cfg, providers_map, cfg.llm.default)
    h.service = BotService(
        cfg,
        h.api,
        h.store,
        extractor,
        BitableWriter(h.api, h.store, sleep=lambda s: None),
        clock=clock,
        query_service=qsvc,
    )
    h.clock = clock
    return h


_seq = [0]


def make_msg(
    text: str,
    *,
    sender="ou_alice",
    chat="oc_1",
    mentions=None,
    root_id="",
    parent_id="",
    message_id=None,
    event_id=None,
    chat_type="group",
    at_bot=True,
    thread_id="",
) -> IncomingMessage:
    _seq[0] += 1
    others = [Mention(f"@_user_{i + 2}", n, oid) for i, (n, oid) in enumerate(mentions or [])]
    ms = ([Mention("@_user_1", "记录机器人", "ou_bot")] if at_bot else []) + others
    prefix = "@_user_1 " if at_bot else ""
    return IncomingMessage(
        event_id=event_id or f"ev{_seq[0]}",
        message_id=message_id or f"om_{_seq[0]}",
        chat_id=chat,
        chat_type=chat_type,
        sender_open_id=sender,
        message_type="text",
        content=json.dumps({"text": f"{prefix}{text}"}, ensure_ascii=False),
        mentions=ms,
        root_id=root_id,
        parent_id=parent_id,
        thread_id=thread_id,
    )
