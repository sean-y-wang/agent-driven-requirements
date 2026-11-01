"""Bitable writer: field mapping, idempotency (client_token + local index), retry, readable errors."""

from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from ..config.models import Role
from .feishu_api import FeishuAPI, FeishuAPIError

log = structlog.get_logger(__name__)

# Feishu rate-limit / transient codes
RETRYABLE_CODES = {99991400, 1254290, 1254291, 1254607, 1255040}
PERMISSION_CODES = {91403, 1254302, 1254304}
FIELD_CODES = {1254045, 1254060, 1254061, 1254062, 1254063, 1254064, 1254065, 1254066, 1254067, 1254068}

_NS = uuid.UUID("6f1f4a52-0b8e-4b5c-9d3f-6f2b8f8a7c11")


def client_token_for(source_message_id: str) -> str:
    # Feishu requires UUIDv4 format; keep it deterministic by stamping v4 bits onto a uuid5 digest.
    return str(uuid.UUID(bytes=uuid.uuid5(_NS, source_message_id).bytes, version=4))


def record_url(role: Role, record_id: str, base: str = "https://feishu.cn") -> str:
    return f"{base}/base/{role.target.app_token}?table={role.target.table_id}&record={record_id}"


class WriteError(Exception):
    def __init__(self, user_message: str, code: int | None = None) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code


def friendly(err: FeishuAPIError) -> str:
    if err.code in PERMISSION_CODES:
        return "机器人没有这张多维表格的写入权限：请在多维表格右上角「…→ 更多 → 添加文档应用」中把本应用添加为可编辑协作者。"
    if err.code in FIELD_CODES:
        return f"多维表格字段不存在或类型不匹配，请联系管理员核对模板配置（{err.msg}）。"
    return f"写入多维表格失败（错误码 {err.code}）：{err.msg}"


@dataclass
class WriteResult:
    record_id: str
    url: str
    duplicated: bool = False


class BitableWriter:
    def __init__(
        self, api: FeishuAPI, store, max_retries: int = 3, sleep=time.sleep, base_url: str = "https://feishu.cn"
    ) -> None:
        self.api = api
        self.store = store
        self.max_retries = max_retries
        self._sleep = sleep
        self.base_url = base_url

    @staticmethod
    def map_fields(role: Role, values: dict[str, Any]) -> dict[str, Any]:
        """key -> Bitable column name. Only columns declared in the template can be written."""
        by_key = {f.key: f.field for f in role.fields}
        return {by_key[k]: v for k, v in values.items() if k in by_key}

    def write(
        self, role: Role, values: dict[str, Any], source_message_id: str, *, check_existing: bool = True
    ) -> WriteResult:
        # Remote upsert callers already checked the authoritative table; a stale local index
        # must not suppress recreation after a remote deletion. Keep token/retry behavior shared.
        existing = self.store.get_record(source_message_id) if check_existing else None
        if existing:
            return WriteResult(existing[0], existing[1], duplicated=True)
        payload = self.map_fields(role, values)
        token = client_token_for(source_message_id)
        attempt = 0
        while True:
            try:
                rid = self.api.create_record(role.target.app_token, role.target.table_id, payload, token)
                break
            except FeishuAPIError as e:
                transient = e.code in RETRYABLE_CODES or (e.http_status or 0) == 429 or (e.http_status or 0) >= 500
                if transient and attempt < self.max_retries:
                    attempt += 1
                    delay = min(8.0, 0.5 * 2 ** (attempt - 1)) + random.random() * 0.1
                    log.warning("bitable.retry", code=e.code, attempt=attempt, delay=round(delay, 2))
                    self._sleep(delay)
                    continue
                log.error("bitable.write_failed", code=e.code, msg=e.msg, table=role.target.table_id)
                raise WriteError(friendly(e), e.code) from e
        url = record_url(role, rid, self.base_url)
        self.store.put_record(source_message_id, rid, url)
        return WriteResult(rid, url)
