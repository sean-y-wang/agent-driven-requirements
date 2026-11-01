"""Personal OAuth client. Never falls back to a bot/tenant credential."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..writer.feishu_api import FeishuAPIError


def cell_text(value) -> str:
    if isinstance(value, list):
        return "".join(str(v.get("text", "")) for v in value if isinstance(v, dict))
    return str(value or "")


class UserAPI:
    def __init__(self, token: str, domain="https://open.feishu.cn"):
        if not token:
            raise ValueError("未登录：请设置 FEISHU_USER_ACCESS_TOKEN（本人 OAuth），不能使用机器人凭证。")
        if domain not in ("https://open.feishu.cn", "https://open.larksuite.com"):
            raise ValueError("unsupported Feishu domain")
        self._token = token
        self.domain = domain

    def request(self, method, path, *, params=None, body=None):
        url = self.domain + "/open-apis" + path
        if params:
            url += "?" + urlencode(params)
        req = Request(
            url,
            method=method,
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with urlopen(req, timeout=30) as response:
                result = json.load(response)
        except HTTPError as e:
            raise FeishuAPIError(e.code, "本人授权失效或没有表权限，请重新登录并联系管理员检查授权。", e.code) from None
        except (URLError, TimeoutError, ValueError):
            raise FeishuAPIError(-1, "飞书连接或响应异常，请稍后重试。") from None
        if result.get("code") != 0:
            # Avoid echoing raw platform payloads that might contain credential material.
            raise FeishuAPIError(result.get("code", -1), "本人授权或表操作失败，请检查登录、权限与中文字段配置。")
        return result.get("data") or {}

    def identity(self) -> str:
        oid = self.request("GET", "/authen/v1/user_info").get("open_id")
        if not oid:
            raise ValueError("无法确认本人身份；拒绝写表。")
        return oid

    @staticmethod
    def path(app_token, table_id):
        # Resource identifiers must never turn into URL path/query injection.
        if not app_token.isalnum() or not table_id.isalnum():
            raise ValueError("invalid table identifier")
        return f"/bitable/v1/apps/{app_token}/tables/{table_id}/records"

    def list_all_records(self, app_token, table_id):
        rows, seen, cursor = [], set(), ""
        while True:
            params = {"page_size": 100, "user_id_type": "open_id"}
            if cursor:
                params["page_token"] = cursor
            data = self.request("GET", self.path(app_token, table_id), params=params)
            rows.extend(data.get("items") or [])
            if not data.get("has_more"):
                return rows
            cursor = data.get("page_token")
            if not cursor or cursor in seen:
                raise ValueError("飞书分页不完整；拒绝按不完整数据写表。")
            seen.add(cursor)

    def create_record(self, app_token, table_id, fields, client_token):
        data = self.request(
            "POST",
            self.path(app_token, table_id),
            params={"user_id_type": "open_id", "client_token": client_token},
            body={"fields": fields},
        )
        return data["record"]["record_id"]

    def update_record(self, app_token, table_id, record_id, fields):
        if not record_id.isalnum():
            raise ValueError("invalid record identifier")
        self.request(
            "PUT",
            self.path(app_token, table_id) + "/" + record_id,
            params={"user_id_type": "open_id"},
            body={"fields": fields},
        )
