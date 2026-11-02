"""Consume report outbox and deliver Feishu cards/texts with (id, revision) dedup + retries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ..config import ConfigError
from ..writer import FeishuAPIError
from .reports import ReportInput, ReportStore, scheduled_payloads

# Feishu text content practical limit; keep headroom under common 4k–10k caps.
CHUNK_SIZE = 3500
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_SLEEP = 0.05  # tests; production CLI can raise via --retry-sleep


class DeliveryAPI(Protocol):
    def send_card(self, receive_id: str, card: dict[str, Any], *, receive_id_type: str = "open_id") -> str: ...

    def send_text(self, receive_id: str, text: str, *, receive_id_type: str = "open_id") -> str: ...


class DeliveryLedger:
    """Persistent send ledger keyed by (id, revision, recipient)."""

    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS delivery_ledger (
                id TEXT NOT NULL,
                revision TEXT NOT NULL,
                recipient TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (id, revision, recipient)
            )"""
        )

    def get(self, item_id: str, revision: str, recipient: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM delivery_ledger WHERE id=? AND revision=? AND recipient=?",
            (item_id, revision, recipient),
        ).fetchone()
        return dict(row) if row else None

    def mark(
        self,
        item_id: str,
        revision: str,
        recipient: str,
        kind: str,
        *,
        status: str,
        attempts: int,
        message_id: str = "",
        last_error: str = "",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """INSERT INTO delivery_ledger
               (id, revision, recipient, kind, status, attempts, last_error, message_id, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id, revision, recipient) DO UPDATE SET
                 kind=excluded.kind,
                 status=excluded.status,
                 attempts=excluded.attempts,
                 last_error=excluded.last_error,
                 message_id=excluded.message_id,
                 updated_at=excluded.updated_at""",
            (item_id, revision, recipient, kind, status, attempts, last_error[:500], message_id, now),
        )

    def close(self) -> None:
        self.db.close()


def chunk_text(body: str, size: int = CHUNK_SIZE) -> list[str]:
    text = (body or "").strip()
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


def recipient_target(recipient: dict[str, str]) -> tuple[str, str]:
    """Pick (receive_id, receive_id_type) without contact lookups.

    Preference: open_id > chat_id > email. Email is sent directly with
    receive_id_type=email so the bot needs no contact:user.id:readonly scope
    (xfchat returns 99991672 for batch_get_id without it).
    """
    if recipient.get("open_id"):
        return recipient["open_id"], "open_id"
    if recipient.get("chat_id"):
        return recipient["chat_id"], "chat_id"
    if recipient.get("email"):
        return recipient["email"], "email"
    return "", "open_id"


def manager_delivery_items(manager: dict | None, recipients: list[dict[str, str]]) -> list[dict]:
    """Turn manager weekly payload into dedupable delivery items (one per recipient)."""
    if not manager or not recipients:
        return []
    period = manager.get("period") or "unknown"
    body = manager.get("body") or ""
    revision = hashlib.sha256(body.encode()).hexdigest()[:16]
    item_id = f"manager:{period}"
    items = []
    for recipient in recipients:
        rid, receive_id_type = recipient_target(recipient)
        if not rid:
            continue
        items.append(
            {
                "id": item_id,
                "revision": revision,
                "recipient": rid,
                "receive_id_type": receive_id_type,
                "kind": "manager",
                "body": body,
                "missing": manager.get("missing") or [],
                "risks": manager.get("risks") or [],
                "period": period,
            }
        )
    return items


def normalize_confirmations(confirmations: list[dict]) -> list[dict]:
    out = []
    for item in confirmations or []:
        if not item.get("id") or not item.get("revision") or not item.get("recipient"):
            continue
        if item.get("status") and item["status"] != "pending":
            continue
        out.append({**item, "receive_id_type": item.get("receive_id_type") or "open_id", "kind": item.get("kind") or "daily"})
    return out


def deliver_outbox(
    outbox: dict,
    api: DeliveryAPI | None,
    ledger: DeliveryLedger,
    *,
    manager_recipients: list[dict[str, str]] | None = None,
    dry_run: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_sleep: float = DEFAULT_RETRY_SLEEP,
) -> dict:
    """Send pending confirmations and manager reports. Dedup by (id, revision, recipient)."""
    results: list[dict] = []
    confirmations = normalize_confirmations(outbox.get("confirmations") or [])
    managers = manager_delivery_items(outbox.get("manager"), manager_recipients or [])

    for item in confirmations + managers:
        key = (item["id"], item["revision"], item["recipient"])
        existing = ledger.get(*key)
        if existing and existing["status"] == "sent":
            results.append(
                {
                    "id": item["id"],
                    "revision": item["revision"],
                    "recipient": item["recipient"],
                    "kind": item["kind"],
                    "status": "skipped",
                    "reason": "already_sent",
                    "message_id": existing.get("message_id") or "",
                }
            )
            continue
        attempts = int(existing["attempts"]) if existing else 0
        if existing and existing["status"] == "failed" and attempts >= max_attempts:
            results.append(
                {
                    "id": item["id"],
                    "revision": item["revision"],
                    "recipient": item["recipient"],
                    "kind": item["kind"],
                    "status": "skipped",
                    "reason": "max_attempts",
                    "attempts": attempts,
                    "last_error": existing.get("last_error") or "",
                }
            )
            continue

        if dry_run or api is None:
            message_ids = ["dry-run"]
            status = "dry_run"
            err = ""
        else:
            status = "failed"
            err = ""
            message_ids = []
            remaining = max(1, max_attempts - attempts)
            for _ in range(remaining):
                attempts += 1
                try:
                    message_ids = []
                    if item["kind"] == "manager":
                        chunks = chunk_text(item["body"])
                        if not chunks:
                            chunks = ["（空管理者周报）"]
                        for idx, chunk in enumerate(chunks):
                            prefix = (
                                f"[管理者周报 {item['period']} · {idx + 1}/{len(chunks)}]\n"
                                if len(chunks) > 1
                                else ""
                            )
                            mid = api.send_text(
                                item["recipient"],
                                prefix + chunk,
                                receive_id_type=item.get("receive_id_type") or "open_id",
                            )
                            message_ids.append(mid)
                    else:
                        mid = api.send_card(
                            item["recipient"],
                            item["card"],
                            receive_id_type=item.get("receive_id_type") or "open_id",
                        )
                        message_ids.append(mid)
                    status = "sent"
                    err = ""
                    break
                except FeishuAPIError as e:
                    status = "failed"
                    err = str(e)
                    if retry_sleep > 0:
                        time.sleep(retry_sleep)
                except Exception as e:  # network / unexpected
                    status = "failed"
                    err = f"{type(e).__name__}: {e}"
                    if retry_sleep > 0:
                        time.sleep(retry_sleep)

        if status == "sent":
            attempts = max(attempts, 1)
            ledger.mark(
                item["id"],
                item["revision"],
                item["recipient"],
                item["kind"],
                status="sent",
                attempts=attempts,
                message_id=message_ids[0] if message_ids else "",
            )
        elif status == "failed":
            ledger.mark(
                item["id"],
                item["revision"],
                item["recipient"],
                item["kind"],
                status="failed",
                attempts=attempts,
                last_error=err,
            )
        elif status == "dry_run":
            # Record dry-run as sent-equivalent only in-memory callers that want; do not persist as sent.
            pass

        results.append(
            {
                "id": item["id"],
                "revision": item["revision"],
                "recipient": item["recipient"],
                "kind": item["kind"],
                "status": status,
                "attempts": attempts if status != "dry_run" else 0,
                "message_ids": message_ids,
                "error": err,
            }
        )

    summary = {
        "sent": sum(1 for r in results if r["status"] == "sent"),
        "dry_run": sum(1 for r in results if r["status"] == "dry_run"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "total": len(results),
    }
    return {"results": results, "summary": summary}


def load_manager_recipients(
    path: Path | None = None,
    *,
    env: dict[str, str] | None = None,
    chat_id: str | None = None,
) -> list[dict[str, str]]:
    env = env or os.environ
    recipients: list[dict[str, str]] = []
    if path and path.exists():
        raw = json.loads(path.read_text())
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    recipients.append({"open_id": item})
                elif isinstance(item, dict):
                    if item.get("chat_id") and not item.get("open_id"):
                        recipients.append({"chat_id": item["chat_id"]})
                    elif item.get("open_id"):
                        recipients.append({"open_id": item["open_id"], "name": item.get("name") or ""})
    for oid in (env.get("REPORT_MANAGER_OPEN_IDS") or "").split(","):
        oid = oid.strip()
        if oid and not any(r.get("open_id") == oid for r in recipients):
            recipients.append({"open_id": oid})
    if chat_id:
        recipients.append({"chat_id": chat_id})
    return recipients


def build_lark_delivery_api(env: dict[str, str] | None = None) -> DeliveryAPI:
    """Create a tenant-token Lark client for outbound IM delivery."""
    import lark_oapi as lark

    from ..main import DOMAINS
    from ..writer import LarkFeishuAPI

    env = env or os.environ
    app_id = env.get("FEISHU_APP_ID") or ""
    app_secret = env.get("FEISHU_APP_SECRET") or ""
    if not app_id or not app_secret:
        raise ValueError("发送适配器需要 FEISHU_APP_ID 与 FEISHU_APP_SECRET")
    domain = (env.get("FEISHU_DOMAIN") or "feishu").lower()
    if domain not in DOMAINS:
        raise ValueError("FEISHU_DOMAIN must be 'feishu' or 'lark'")
    open_domain = DOMAINS[domain][0]
    client = (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .domain(open_domain)
        .build()
    )
    return LarkFeishuAPI(client)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成并/或发送日报周报确认卡与管理者周报（按 id+revision 去重，失败可重试）"
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--outbox", type=Path, help="已有 outbox JSON（requirements-reports 输出）")
    source.add_argument("--input", type=Path, help="离线 members+progress JSON，先生成再发送")
    source.add_argument("--config", type=Path, help="真实四表配置；需 FEISHU_USER_ACCESS_TOKEN 与 --members")
    parser.add_argument("--members", type=Path, help="成员清单 JSON")
    parser.add_argument("--manager-recipients", type=Path, help="管理者接收人 JSON（open_id 或 chat_id）")
    parser.add_argument("--manager-chat", help="管理者周报群 chat_id（可选）")
    parser.add_argument("--now", help="带时区 ISO 时间")
    parser.add_argument("--timezone")
    parser.add_argument("--db", type=Path, default=Path("data/reports.db"), help="汇报草稿库（与 bot 共用）")
    parser.add_argument(
        "--ledger",
        type=Path,
        help="投递账本 SQLite；默认与 --db 同库（delivery_ledger 表）",
    )
    parser.add_argument("--dry-run", action="store_true", help="生成并演练发送，不调用飞书、不写投递成功")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--retry-sleep", type=float, default=DEFAULT_RETRY_SLEEP)
    parser.add_argument("--write-outbox", type=Path, help="可选：把本次 outbox 落到文件")
    args = parser.parse_args(argv)

    try:
        manager_recipients = load_manager_recipients(
            args.manager_recipients, chat_id=args.manager_chat
        )
        if args.outbox:
            outbox = json.loads(args.outbox.read_text())
        elif args.input or args.config:
            # Reuse reports generator without printing; call scheduled_payloads directly.
            timezone = args.timezone or "Asia/Shanghai"
            if args.input:
                data = ReportInput.model_validate_json(args.input.read_text())
            else:
                if not args.members:
                    raise ValueError("读取真实进展表需 --members")
                from ..config import load_config
                from .user_api import UserAPI

                cfg = load_config(args.config)
                if not cfg.tables:
                    raise ValueError("汇报需要 0.9 四表配置")
                timezone = args.timezone or cfg.timezone
                api_user = UserAPI(os.environ.get("FEISHU_USER_ACCESS_TOKEN", ""))
                api_user.identity()
                table = cfg.tables.progress
                from .reports import entries_from_records

                entries = entries_from_records(api_user.list_all_records(table.app_token, table.table_id), table)
                data = ReportInput(members=json.loads(args.members.read_text()), progress=entries)
            from datetime import datetime
            from zoneinfo import ZoneInfo

            now = datetime.fromisoformat(args.now) if args.now else datetime.now(ZoneInfo(timezone))
            store = ReportStore() if args.dry_run else ReportStore(args.db)
            try:
                outbox = scheduled_payloads(data, now, store, timezone)
            finally:
                store.close()
        else:
            raise ValueError("请提供 --outbox，或 --input / --config 以生成 outbox")

        if args.write_outbox:
            args.write_outbox.parent.mkdir(parents=True, exist_ok=True)
            args.write_outbox.write_text(json.dumps(outbox, ensure_ascii=False, indent=2) + "\n")

        ledger_path = args.ledger or args.db
        # dry-run uses memory ledger so we never persist "sent"
        ledger = DeliveryLedger() if args.dry_run else DeliveryLedger(ledger_path)
        try:
            api = None if args.dry_run else build_lark_delivery_api()
            result = deliver_outbox(
                outbox,
                api,
                ledger,
                manager_recipients=manager_recipients,
                dry_run=args.dry_run,
                max_attempts=args.max_attempts,
                retry_sleep=args.retry_sleep,
            )
            print(json.dumps({"outbox_counts": {
                "confirmations": len(outbox.get("confirmations") or []),
                "manager": 1 if outbox.get("manager") else 0,
            }, **result}, ensure_ascii=False, indent=2))
            return 0 if result["summary"]["failed"] == 0 else 2
        finally:
            ledger.close()
    except (ValueError, OSError, FeishuAPIError, ConfigError, json.JSONDecodeError) as e:
        print(f"requirements-deliver: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
