"""Thin wrapper over lark-oapi OpenAPI calls used by the bot. Everything else talks to the Protocol."""

from __future__ import annotations

import json
from typing import Any, Protocol


class FeishuAPIError(Exception):
    def __init__(self, code: int, msg: str, http_status: int | None = None) -> None:
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.http_status = http_status


class FeishuAPI(Protocol):
    def list_all_records(self, app_token: str, table_id: str) -> list[dict[str, Any]]: ...

    def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None: ...

    def create_record(self, app_token: str, table_id: str, fields: dict[str, Any], client_token: str) -> str: ...

    def reply_text(self, message_id: str, text: str, in_thread: bool = False) -> str: ...

    def reply_card(self, message_id: str, card: dict[str, Any], in_thread: bool = False) -> str: ...

    def list_chat_members(self, chat_id: str) -> list[tuple[str, str]]: ...

    # ---- context / traceability (all optional: failures degrade gracefully) ----
    def get_bot_open_id(self) -> str: ...

    def get_message(self, message_id: str) -> dict[str, Any]: ...

    def list_messages(
        self, container_type: str, container_id: str, page_size: int, newest_first: bool = True
    ) -> list[dict[str, Any]]: ...

    def get_user_name(self, open_id: str) -> str: ...

    def get_chat_name(self, chat_id: str) -> str: ...

    def search_records(
        self, app_token: str, table_id: str, filter_info: dict[str, Any], page_size: int = 20
    ) -> list[dict[str, Any]]: ...

    def list_records(self, app_token: str, table_id: str, page_size: int = 30) -> list[dict[str, Any]]: ...

    def add_reaction(self, message_id: str, emoji_type: str = "Get") -> None: ...


class LarkFeishuAPI:
    """Production implementation backed by lark_oapi.Client (handles tenant_access_token)."""

    def __init__(self, client: Any) -> None:
        self.client = client

    @staticmethod
    def _check(resp: Any) -> None:
        if not resp.success():
            raw = getattr(resp, "raw", None)
            raise FeishuAPIError(resp.code, resp.msg, getattr(raw, "status_code", None))

    def create_record(self, app_token: str, table_id: str, fields: dict[str, Any], client_token: str) -> str:
        from lark_oapi.api.bitable.v1 import AppTableRecord, CreateAppTableRecordRequest

        req = (
            CreateAppTableRecordRequest.builder()
            .app_token(app_token)
            .table_id(table_id)
            .user_id_type("open_id")
            .client_token(client_token)
            .request_body(AppTableRecord.builder().fields(fields).build())
            .build()
        )
        resp = self.client.bitable.v1.app_table_record.create(req)
        self._check(resp)
        return resp.data.record.record_id

    def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
        from lark_oapi.api.bitable.v1 import AppTableRecord, UpdateAppTableRecordRequest

        req = (UpdateAppTableRecordRequest.builder().app_token(app_token).table_id(table_id)
               .record_id(record_id).user_id_type("open_id")
               .request_body(AppTableRecord.builder().fields(fields).build()).build())
        self._check(self.client.bitable.v1.app_table_record.update(req))

    def list_all_records(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        rows, seen, cursor = [], set(), ""
        while True:
            queries = [("page_size", "100"), ("user_id_type", "open_id")]
            if cursor:
                queries.append(("page_token", cursor))
            data = self._get_json(f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records", queries)
            rows.extend(data.get("items") or [])
            if not data.get("has_more"):
                return rows
            cursor = data.get("page_token")
            if not cursor or cursor in seen:
                raise FeishuAPIError(-1, "飞书分页不完整，已停止读取")
            seen.add(cursor)

    def reply_text(self, message_id: str, text: str, in_thread: bool = False) -> str:
        return self._reply(message_id, "text", {"text": text}, in_thread)

    def reply_card(self, message_id: str, card: dict[str, Any], in_thread: bool = False) -> str:
        return self._reply(message_id, "interactive", card, in_thread)

    def _reply(self, message_id: str, msg_type: str, content: dict[str, Any], in_thread: bool) -> str:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .reply_in_thread(in_thread)
                .build()
            )
            .build()
        )
        resp = self.client.im.v1.message.reply(req)
        self._check(resp)
        return resp.data.message_id

    def list_chat_members(self, chat_id: str) -> list[tuple[str, str]]:
        from lark_oapi.api.im.v1 import GetChatMembersRequest

        out: list[tuple[str, str]] = []
        page_token = None
        while True:
            b = GetChatMembersRequest.builder().chat_id(chat_id).member_id_type("open_id").page_size(100)
            if page_token:
                b = b.page_token(page_token)
            resp = self.client.im.v1.chat_members.get(b.build())
            self._check(resp)
            out += [(m.name, m.member_id) for m in resp.data.items or []]
            if not resp.data.has_more:
                return out
            page_token = resp.data.page_token

    # ---- raw JSON calls (keep unknown fields such as message_app_link / message_position) ----
    def _get_json(self, uri: str, queries: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        import lark_oapi as lark

        req = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri(uri)
            .token_types({lark.AccessTokenType.TENANT})
            .queries(queries or [])
            .build()
        )
        resp = self.client.request(req)
        raw = getattr(resp, "raw", None)
        try:
            body = json.loads(raw.content) if raw is not None and raw.content else {}
        except (ValueError, TypeError):
            body = {}
        code = body.get("code", resp.code if resp.code is not None else -1)
        if code != 0:
            raise FeishuAPIError(
                code, body.get("msg") or resp.msg or "request failed", getattr(raw, "status_code", None)
            )
        return body.get("data") or {}

    def get_bot_open_id(self) -> str:
        import lark_oapi as lark

        req = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        resp = self.client.request(req)
        body = json.loads(resp.raw.content) if getattr(resp, "raw", None) is not None else {}
        if body.get("code") != 0:
            raise FeishuAPIError(body.get("code", -1), body.get("msg", "bot info failed"))
        return (body.get("bot") or {}).get("open_id", "")

    def get_message(self, message_id: str) -> dict[str, Any]:
        items = self._get_json(f"/open-apis/im/v1/messages/{message_id}").get("items") or []
        return items[0] if items else {}

    def list_messages(
        self, container_type: str, container_id: str, page_size: int, newest_first: bool = True
    ) -> list[dict[str, Any]]:
        q = [
            ("container_id_type", container_type),
            ("container_id", container_id),
            ("page_size", str(max(1, min(50, page_size)))),
            ("sort_type", "ByCreateTimeDesc" if newest_first else "ByCreateTimeAsc"),
        ]
        return self._get_json("/open-apis/im/v1/messages", q).get("items") or []

    def get_user_name(self, open_id: str) -> str:
        data = self._get_json(f"/open-apis/contact/v3/users/{open_id}", [("user_id_type", "open_id")])
        return (data.get("user") or {}).get("name", "")

    def get_chat_name(self, chat_id: str) -> str:
        return self._get_json(f"/open-apis/im/v1/chats/{chat_id}").get("name", "")

    def search_records(
        self, app_token: str, table_id: str, filter_info: dict[str, Any], page_size: int = 20
    ) -> list[dict[str, Any]]:
        from lark_oapi.api.bitable.v1 import (
            Condition,
            FilterInfo,
            SearchAppTableRecordRequest,
            SearchAppTableRecordRequestBody,
        )

        conditions = [
            Condition.builder().field_name(c["field_name"]).operator(c["operator"]).value(c.get("value") or []).build()
            for c in (filter_info.get("conditions") or [])
        ]
        filt = FilterInfo.builder().conjunction(filter_info.get("conjunction") or "or").conditions(conditions).build()
        body = SearchAppTableRecordRequestBody.builder().filter(filt).automatic_fields(False).build()
        req = (
            SearchAppTableRecordRequest.builder()
            .app_token(app_token)
            .table_id(table_id)
            .page_size(max(1, min(100, page_size)))
            .user_id_type("open_id")
            .request_body(body)
            .build()
        )
        resp = self.client.bitable.v1.app_table_record.search(req)
        self._check(resp)
        items = []
        for rec in resp.data.items or []:
            # Normalize to dict shape used by query layer (fields + record_id)
            fields = getattr(rec, "fields", None) or {}
            if hasattr(fields, "map"):
                fields = dict(fields.map) if fields.map is not None else {}
            elif not isinstance(fields, dict):
                fields = dict(fields) if fields else {}
            items.append({"record_id": getattr(rec, "record_id", "") or "", "fields": fields})
        return items

    def list_records(self, app_token: str, table_id: str, page_size: int = 30) -> list[dict[str, Any]]:
        data = self._get_json(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            [("page_size", str(max(1, min(100, page_size)))), ("user_id_type", "open_id")],
        )
        return data.get("items") or []

    def add_reaction(self, message_id: str, emoji_type: str = "Get") -> None:
        """Best-effort message reaction (e.g. 「收到」). Needs im:message.reaction scope."""
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                .build()
            )
            .build()
        )
        resp = self.client.im.v1.message_reaction.create(req)
        self._check(resp)
