"""Durable local confirmation drafts and a scheduler/outbox interface; no draft Bitable."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import threading
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from ..config import ConfigError, load_config
from ..writer import FeishuAPIError
from .user_api import UserAPI, cell_text


class Member(BaseModel):
    open_id: str
    name: str


class ProgressEntry(BaseModel):
    author: str
    day: date
    task: str = "未关联任务"
    module: str = "未映射模块"
    summary: str
    risk: str = ""
    review: bool = False
    agent: str = "其他"
    record_id: str = ""


class ReportInput(BaseModel):
    members: list[Member]
    progress: list[ProgressEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_members(self):
        ids = [m.open_id for m in self.members]
        if any(not oid for oid in ids) or len(ids) != len(set(ids)):
            raise ValueError("汇报成员 open_id 必须非空且唯一")
        return self


def entries_from_records(rows, table) -> list[ProgressEntry]:
    cols = {f.key: f.field for f in table.fields}
    entries = []
    for row in rows:
        fields = row.get("fields") or {}

        def value(key, fields=fields):
            return cell_text(fields.get(cols.get(key, "")))

        for person in fields.get(cols["author"]) or []:
            entries.append(
                ProgressEntry(
                    record_id=row.get("record_id", ""),
                    author=person.get("id", person.get("open_id", "")),
                    day=value("day"),
                    task=value("task") or "未关联任务",
                    module=value("module") or "未映射模块",
                    summary=value("summary"),
                    risk=value("risk"),
                    review=bool(fields.get(cols["review"])),
                    agent=value("agent"),
                )
            )
    return entries


def due_kinds(now: datetime, timezone="Asia/Shanghai") -> list[str]:
    if now.tzinfo is None:
        raise ValueError("调度时间必须带时区")
    local = now.astimezone(ZoneInfo(timezone))
    due = []
    if local.weekday() < 5 and local.time() >= time(9):
        due.append("daily")
    if local.weekday() == 4 and local.time() >= time(18):
        due.append("weekly")
    return due


def period(kind: str, today: date) -> tuple[date, date]:
    if kind == "weekly":
        return today - timedelta(days=today.weekday()), today
    if kind != "daily":
        raise ValueError("unknown report kind")
    end = today - timedelta(days=1)
    start = end
    while start.weekday() >= 5:
        start -= timedelta(days=1)
    return start, end


def personal_body(member: Member, rows: list[ProgressEntry], start: date, end: date) -> str:
    lines = [f"{member.name}（{start} 至 {end}）"]
    if not rows:
        lines.append("本期还没有进展。可点「改摘要」写下今天做了什么，或点「整单确认」表示本期无进展。")
    for row in sorted(rows, key=lambda r: (r.day, r.module, r.task, r.agent)):
        suffix = "；待复核" if row.review else ""
        risk = f"；风险：{row.risk}" if row.risk else ""
        lines.append(f"- {row.day}｜{row.module}｜{row.task}｜{row.summary}（{row.agent}）{suffix}{risk}")
    return "\n".join(lines)


def confirmation_card(draft: dict) -> dict:
    name = "日报" if draft["kind"] == "daily" else "周报"
    note = "上期未确认，已重新拟定。\n" if draft["redrafted"] else ""
    value = {"kind": "report_confirm", "report_id": draft["id"], "revision": draft["revision"]}
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"请确认个人{name}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content": note + draft["body"]}},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "一次只处理本份。有内容直接「整单确认」；要改先点「改摘要」；没有进展也可以确认。",
                    }
                ],
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": style,
                        "text": {"tag": "plain_text", "content": label},
                        "value": {**value, "action": action},
                    }
                    for action, label, style in [
                        ("confirm", "整单确认", "primary"),
                        ("edit", "改摘要", "default"),
                        ("void", "作废", "danger"),
                    ]
                ],
            },
        ],
    }


def personal_entries(entries):
    """Group progress and review-required rows never enter a personal confirmation."""
    return [r for r in entries if r.agent != "群" and not r.review]


class ReportStore:
    """SQLite is the confirmation authority; confirmed bodies are immutable snapshots."""

    def __init__(self, path=":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS report_drafts (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, day TEXT NOT NULL,
            body TEXT NOT NULL, revision TEXT NOT NULL, status TEXT NOT NULL, redrafted INTEGER NOT NULL
        )""")
        # Additive migration of 0.9 databases, shared by bot, scheduler and PA.
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                columns = {r[1] for r in self.db.execute("PRAGMA table_info(report_drafts)")}
                for name, default in [("entries", "[]"), ("source_hash", ""), ("edited", "0")]:
                    if name not in columns:
                        self.db.execute(
                            f"ALTER TABLE report_drafts ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'"
                        )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        result["entries"] = json.loads(result["entries"])
        return result

    def get(self, report_id):
        with self.lock:
            return self.decode(self.db.execute("SELECT * FROM report_drafts WHERE id=?", (report_id,)).fetchone())

    def draft(self, owner, kind, day, body, entries=None, *, manual=False):
        if not owner or kind not in ("daily", "weekly") or not body.strip():
            raise ValueError("汇报需要本人身份、类型和非空摘要")
        day = date.fromisoformat(day).isoformat()
        rid = hashlib.sha256(f"{owner}:{kind}:{day}".encode()).hexdigest()[:24]
        serialized = json.dumps(entries or [], ensure_ascii=False, sort_keys=True)
        source_hash = hashlib.sha256((body + serialized).encode()).hexdigest()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old = self.get(rid)
                if old and (old["status"] != "pending" or old["source_hash"] == source_hash or old["edited"] == "1"):
                    result = old
                else:
                    previous = self.db.execute(
                        "SELECT id FROM report_drafts WHERE owner=? AND kind=? AND day<? AND status='pending'",
                        (owner, kind, day),
                    ).fetchall()
                    self.db.execute(
                        "UPDATE report_drafts SET status='superseded' WHERE owner=? AND kind=? AND day<? AND status='pending'",
                        (owner, kind, day),
                    )
                    redrafted = bool(previous) or bool(old and old["redrafted"])
                    self.db.execute(
                        """INSERT OR REPLACE INTO report_drafts
                        (id,owner,kind,day,body,revision,status,redrafted,entries,source_hash,edited)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            rid,
                            owner,
                            kind,
                            day,
                            body,
                            uuid.uuid4().hex[:16],
                            "pending",
                            int(redrafted),
                            serialized,
                            source_hash,
                            "1" if manual else "0",
                        ),
                    )
                    result = self.get(rid)
                self.db.execute("COMMIT")
                return result
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    @staticmethod
    def valid(draft, revision, operator, today):
        if not draft or not operator or draft["owner"] != operator or draft["revision"] != revision:
            return False
        day = date.fromisoformat(draft["day"])
        # Daily: valid through the next calendar day (~1 day window). Weekly: through Sunday.
        expiry = day + timedelta(days=1) if draft["kind"] == "daily" else day + timedelta(days=6 - day.weekday())
        return draft["status"] == "pending" and day <= today <= expiry

    def act(self, report_id, revision, operator, today, action, *, body=None, edits=None):
        """Atomic owner/revision/expiry check plus mutation. Never trust an owner from an action value."""
        if action not in ("confirm", "edit", "void"):
            raise ValueError("未知确认操作")
        if action != "edit" and (body is not None or edits is not None):
            raise ValueError("确认/作废不能夹带未展示的摘要修改")
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                draft = self.get(report_id)
                if not self.valid(draft, revision, operator, today):
                    raise ValueError("确认已过期、已处理、版本已变化或非本人操作")
                if action == "edit":
                    if body is not None and edits is not None:
                        raise ValueError("整篇摘要与逐条修改不能同时提交")
                    if edits is not None:
                        if not edits or not isinstance(edits, dict):
                            raise ValueError("逐条修改需要条目索引与摘要")
                        rows = [ProgressEntry.model_validate(r) for r in draft["entries"]]
                        for index, summary in edits.items():
                            if not str(index).isdigit() or int(index) >= len(rows):
                                raise ValueError("条目不存在")
                            if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
                                raise ValueError("条目摘要必须为 1–4000 字")
                            rows[int(index)].summary = summary.strip()
                        draft["entries"] = [r.model_dump(mode="json") for r in rows]
                        start, end = period(draft["kind"], date.fromisoformat(draft["day"]))
                        body = personal_body(Member(open_id=operator, name=operator), rows, start, end)
                    if not isinstance(body, str) or not body.strip() or len(body) > 20000:
                        raise ValueError("摘要请写 1–20000 字；没有编码任务时直接写今天做了什么即可")
                    draft["body"] = body.strip()
                    draft["revision"] = uuid.uuid4().hex[:16]
                    draft["edited"] = "1"
                    # Whole-body edits replace the old content, including its risk metadata.
                    if edits is None:
                        draft["entries"] = []
                else:
                    draft["status"] = "confirmed" if action == "confirm" else "void"
                self.db.execute(
                    "UPDATE report_drafts SET body=?,revision=?,status=?,entries=?,edited=? WHERE id=?",
                    (
                        draft["body"],
                        draft["revision"],
                        draft["status"],
                        json.dumps(draft["entries"], ensure_ascii=False),
                        draft["edited"],
                        report_id,
                    ),
                )
                self.db.execute("COMMIT")
                return draft
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def confirm(self, report_id: str, revision: str, operator: str, today: date) -> bool:
        try:
            self.act(report_id, revision, operator, today, "confirm")
            return True
        except ValueError:
            return False

    def confirmed_reports(self, kind, start, end):
        with self.lock:
            rows = self.db.execute(
                """SELECT * FROM report_drafts WHERE kind=? AND day>=? AND day<=?
                AND status='confirmed' ORDER BY day,id""",
                (kind, start.isoformat(), end.isoformat()),
            ).fetchall()
        # At most one full report per person in the manager body, latest confirmed submission wins.
        return list({r["owner"]: self.decode(r) for r in rows}.values())

    def confirmed_owners(self, kind: str, day: date) -> set[str]:
        return {r["owner"] for r in self.confirmed_reports(kind, day, day)}

    def close(self):
        self.db.close()


def manager_report(members, entries, today, confirmed, *, snapshots=None) -> dict:
    start, end = period("weekly", today)
    rows = [p for p in personal_entries(entries) if start <= p.day <= end and p.author in confirmed]
    if snapshots is not None:
        rows = [ProgressEntry.model_validate(p) for draft in snapshots for p in draft["entries"]]
        confirmed = {d["owner"] for d in snapshots}
    missing = [m.name for m in members if m.open_id not in confirmed]
    names = {m.open_id: m.name for m in members}
    risks = [f"{names.get(p.author, p.author)}｜{p.task}：{p.risk}" for p in rows if p.risk]
    if snapshots is not None:
        body = "\n\n".join(d["body"] for d in snapshots)
    else:
        body = "\n\n".join(
            personal_body(
                Member(open_id=oid, name=names.get(oid, oid)), [p for p in rows if p.author == oid], start, end
            )
            for oid in sorted(confirmed)
        )
    return {
        "period": f"{start}/{end}",
        "missing": missing,
        "risks": risks,
        "body": f"跨人周报（{start} 至 {end}）\n缺报/未确认：{'、'.join(missing) or '无'}\n风险：\n"
        + ("\n".join(risks) or "无")
        + "\n\n"
        + (body or "暂无已确认周报。"),
    }


def scheduled_payloads(data: ReportInput, now: datetime, store: ReportStore, timezone="Asia/Shanghai") -> dict:
    kinds = due_kinds(now, timezone)
    local = now.astimezone(ZoneInfo(timezone))
    today = local.date()
    out = []
    for kind in kinds:
        start, end = period(kind, today)
        for member in data.members:
            rows = [r for r in personal_entries(data.progress) if r.author == member.open_id and start <= r.day <= end]
            draft = store.draft(
                member.open_id,
                kind,
                today.isoformat(),
                personal_body(member, rows, start, end),
                [r.model_dump(mode="json") for r in rows],
            )
            if draft["status"] == "pending":
                out.append(
                    {
                        "id": draft["id"],
                        "revision": draft["revision"],
                        "recipient": member.open_id,
                        "kind": kind,
                        "delivery": "scheduled",
                        "allow_interrupt": kind == "daily",
                        "card": confirmation_card(draft),
                        "items": draft["entries"],
                        "empty": not rows,
                        "manual_action": "progress.submit",
                        "status": draft["status"],
                    }
                )
    manager = None
    if "weekly" in kinds or (local.weekday() == 0 and "daily" in kinds):
        anchor = today - timedelta(days=1) if local.weekday() == 0 else today
        start, end = period("weekly", anchor)
        snapshots = store.confirmed_reports("weekly", start, end)
        manager = manager_report(data.members, [], anchor, set(), snapshots=snapshots)
    return {"confirmations": out, "manager": manager}


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成日报/周报确认及管理者周报 outbox（不自动发送）")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="离线 JSON: members + progress，使用稳定 open_id")
    source.add_argument("--config", type=Path, help="读取真实进展表；需本人 OAuth 及 --members")
    parser.add_argument("--members", type=Path, help="成员清单 JSON: [{open_id, name}]")
    parser.add_argument("--now", help="带时区的 ISO 时间；默认当前时间")
    parser.add_argument("--timezone")
    parser.add_argument("--db", type=Path, default=Path("data/reports.db"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        timezone = args.timezone or "Asia/Shanghai"
        if args.input:
            data = ReportInput.model_validate_json(args.input.read_text())
        else:
            if not args.members:
                raise ValueError("读取真实进展表需 --members 成员名单")
            cfg = load_config(args.config)
            if not cfg.tables:
                raise ValueError("汇报需要 0.9 四表配置")
            timezone = args.timezone or cfg.timezone
            api = UserAPI(os.environ.get("FEISHU_USER_ACCESS_TOKEN", ""))
            api.identity()
            table = cfg.tables.progress
            entries = entries_from_records(api.list_all_records(table.app_token, table.table_id), table)
            data = ReportInput(members=json.loads(args.members.read_text()), progress=entries)
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(ZoneInfo(timezone))
        store = ReportStore() if args.dry_run else ReportStore(args.db)
        try:
            result = scheduled_payloads(data, now, store, timezone)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0
    except (ValueError, OSError, ConfigError, FeishuAPIError) as e:
        print(f"requirements-reports: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
