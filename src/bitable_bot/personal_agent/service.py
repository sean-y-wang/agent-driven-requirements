"""Personal confirmation tool API. Identity always comes from the authenticated backend."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..requirements_v09.identity import progress_key
from ..requirements_v09.models import MODULES
from ..requirements_v09.progress import match_task
from ..requirements_v09.reports import (
    Member,
    ReportInput,
    ReportStore,
    confirmation_card,
    entries_from_records,
    period,
    personal_body,
    personal_entries,
    scheduled_payloads,
)
from ..requirements_v09.user_api import cell_text
from ..requirements_v09.writer import MultiTableWriter
from ..session import Store
from ..writer import BitableWriter


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProgressSubmit(Input):
    task: str | None = None
    day: date | None = None
    summary: str = Field(min_length=1, max_length=4000)
    source: Literal["Claude Code", "Codex", "Orca", "其他"] = "其他"
    risk: str = Field(default="", max_length=2000)


class ReportPrepare(Input):
    kind: Literal["daily", "weekly"] = "daily"
    body: str | None = Field(default=None, min_length=1, max_length=20000)


class ReportAction(Input):
    report_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    action: Literal["confirm", "edit", "void"] = "confirm"
    body: str | None = Field(default=None, min_length=1, max_length=20000)
    edits: dict[str, str] | None = None


class ReportConfirm(Input):
    report_id: str | None = None
    revision: str | None = None
    body: str | None = Field(default=None, min_length=1, max_length=20000)


class TaskRequest(Input):
    task: str = Field(min_length=1)


class NaturalRequest(Input):
    text: str = Field(min_length=1, max_length=20000)
    task: str | None = None

    @field_validator("text")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("请输入要记录的内容")
        return value


class PersonalAgent:
    """One instance per authenticated user/client. No global caller-supplied user ID."""

    def __init__(self, cfg, api, reports: ReportStore, *, clock=None, actor="worker"):
        if not cfg.tables:
            raise ValueError("个人确认入口需要四表配置")
        self.cfg, self.api, self.reports = cfg, api, reports
        self.clock = clock or (lambda: datetime.now(ZoneInfo(cfg.timezone)))
        self.actor = actor

    def now(self):
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("时间必须带时区")
        return now.astimezone(ZoneInfo(self.cfg.timezone))

    def records(self, name):
        table = self.cfg.tables.get(name)
        return self.api.list_all_records(table.app_token, table.table_id)

    def values(self, name, row):
        return {f.key: row.get("fields", {}).get(f.field) for f in self.cfg.tables.get(name).fields}

    def tasks(self, owner):
        result = []
        for row in self.records("tasks"):
            values = self.values("tasks", row)
            if any(
                p.get("id", p.get("open_id")) == owner
                for p in (values.get("participants") or [])
                if isinstance(p, dict)
            ):
                result.append({"record_id": row["record_id"], **values})
        return result

    def dossier(self, owner, task_id):
        task = next((t for t in self.tasks(owner) if t["record_id"] == task_id), None)
        if task is None:
            raise ValueError("只能读取本人参与的任务档案")
        requirement_id = cell_text(task.get("requirement"))
        requirements = [
            {"record_id": r["record_id"], **self.values("requirements", r)}
            for r in self.records("requirements")
            if r["record_id"] == requirement_id
        ]
        module = cell_text(task.get("module"))
        modules = [
            self.values("modules", r)
            for r in self.records("modules")
            if cell_text(self.values("modules", r).get("module")) == module
        ]
        progress = [
            {"record_id": r["record_id"], **self.values("progress", r)}
            for r in self.records("progress")
            if cell_text(self.values("progress", r).get("task")) == task_id
        ]
        return {"task": task, "requirements": requirements, "modules": modules, "progress": progress, "read_only": True}

    def prepare(self, owner, request):
        today = self.now().date()
        start, end = period(request.kind, today)
        rows = [
            r
            for r in personal_entries(entries_from_records(self.records("progress"), self.cfg.tables.progress))
            if r.author == owner and start <= r.day <= end
        ]
        body = request.body or personal_body(Member(open_id=owner, name=owner), rows, start, end)
        # A manually supplied full report is a new snapshot, not a view of old progress metadata.
        entries = [] if request.body else [r.model_dump(mode="json") for r in rows]
        draft = self.reports.draft(
            owner, request.kind, today.isoformat(), body, entries, manual=request.body is not None
        )
        return self.present(draft)

    @staticmethod
    def present(draft):
        result = {**draft, "items": draft["entries"], "empty": not draft["entries"], "manual_action": "progress.submit"}
        if draft["status"] == "pending":
            result["card"] = confirmation_card(draft)
        return result

    def submit(self, owner, request, *, dry_run):
        if not request.task:
            return {
                "status": "needs_task",
                "message": "请选择「我的任务」；无任务可保留本地候选，不能自动入需求池。",
                "tasks": self.tasks(owner),
                "candidate": {"summary": request.summary, "persisted": False},
            }
        tasks = self.records("tasks")
        row = next((r for r in tasks if r["record_id"] == request.task), None)
        module = cell_text(self.values("tasks", row).get("module")) if row else ""
        if module not in MODULES:
            raise ValueError("任务尚未映射有效模块；未写表")
        match_task(tasks, self.cfg.tables.tasks, owner, module, request.task)
        day = request.day or self.now().date()
        if day > self.now().date():
            raise ValueError("进展日期不能晚于今天")
        key = progress_key(owner, day.isoformat(), request.task, request.source)
        values = {
            "task": request.task,
            "module": module,
            "author": [{"id": owner}],
            "day": day.isoformat(),
            "summary": request.summary,
            "agent": request.source,
            "risk": request.risk,
            "review": False,
            "sync_key": key,
        }
        store = Store()
        try:
            writer = MultiTableWriter(BitableWriter(self.api, store), self.cfg.tables)
            if dry_run:
                return {"dry_run": True, **writer.write("progress", values, key, dry_run=True)}
            result = writer.upsert_progress(values)
            return {"record_id": result.record_id, "updated": result.duplicated, "sync_key": key}
        finally:
            store.close()

    def call(self, method: str, payload: dict | None = None, *, dry_run=False):
        if self.actor.lower() == "master":
            raise ValueError("Master 不写表，也不代本人确认；请由本人确认入口操作")
        if not isinstance(payload, (dict, type(None))):
            raise ValueError("请求必须为 JSON object")
        payload = payload or {}
        owner = self.api.identity()
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("无法确认本人身份")
        if dry_run and method != "progress.submit":
            # Preview existing state, but never mutate the persistent confirmation/session database.
            scratch = ReportStore()
            try:
                with self.reports.lock:
                    self.reports.db.backup(scratch.db)
                client = PersonalAgent(self.cfg, self.api, scratch, clock=self.clock, actor=self.actor)
                return {"dry_run": True, "result": client.call(method, payload)}
            finally:
                scratch.close()
        if method == "progress.submit":
            return self.submit(owner, ProgressSubmit.model_validate(payload), dry_run=dry_run)
        if method == "tasks.list":
            Input.model_validate(payload)
            return {"tasks": self.tasks(owner)}
        if method == "task.open":
            return self.dossier(owner, TaskRequest.model_validate(payload).task)
        if method == "requirements.related":
            Input.model_validate(payload)
            ids = {cell_text(t.get("requirement")) for t in self.tasks(owner)} - {""}
            return {
                "requirements": [
                    {"record_id": r["record_id"], **self.values("requirements", r)}
                    for r in self.records("requirements")
                    if r["record_id"] in ids
                ],
                "read_only": True,
            }
        if method == "report.prepare":
            return self.prepare(owner, ReportPrepare.model_validate(payload))
        if method == "report.action":
            request = ReportAction.model_validate(payload)
            draft = self.reports.act(
                request.report_id,
                request.revision,
                owner,
                self.now().date(),
                request.action,
                body=request.body,
                edits=request.edits,
            )
            return self.present(draft)
        if method in ("daily.confirm", "weekly.confirm"):
            request = ReportConfirm.model_validate(payload)
            kind = method.split(".")[0]
            if request.report_id:
                if not request.revision or request.body is not None:
                    raise ValueError("确认已有草稿必须提供 revision；改摘要请先 report.action")
                draft = self.reports.get(request.report_id)
                if not draft or draft["owner"] != owner or draft["kind"] != kind:
                    raise ValueError("汇报不存在、类型不符或非本人操作")
                revision = request.revision
            else:
                if request.revision or not request.body:
                    raise ValueError("主动提交并确认需要 body；或提供 report_id + revision")
                draft = self.prepare(owner, ReportPrepare(kind=kind, body=request.body))
                if draft["body"] != request.body:
                    raise ValueError("已有不同摘要，请先 report.action 修改后确认")
                revision = draft["revision"]
            return self.present(self.reports.act(draft["id"], revision, owner, self.now().date(), "confirm"))
        if method == "inbox":
            Input.model_validate(payload)
            data = ReportInput(
                members=[Member(open_id=owner, name=owner)],
                progress=entries_from_records(self.records("progress"), self.cfg.tables.progress),
            )
            result = scheduled_payloads(data, self.now(), self.reports, self.cfg.timezone)
            return {
                "confirmations": result["confirmations"],
                "tasks": self.tasks(owner),
                "related_action": "requirements.related",
            }
        if method == "intent.route":
            return route_intent(NaturalRequest.model_validate(payload))
        raise ValueError(f"未知方法：{method}")


def route_intent(request):
    """Conservative language-to-tool proposal; PA can also populate the public JSON schema directly."""
    text = request.text.strip()
    if text in ("我的任务", "看看我的任务"):
        return {"method": "tasks.list", "payload": {}}
    if text.startswith(("开工", "读档案")):
        return {
            "method": "task.open" if request.task else "tasks.list",
            "payload": {"task": request.task} if request.task else {},
        }
    for prefix, kind in [
        ("帮我记一条进展", "progress"),
        ("记一条进展", "progress"),
        ("补日报", "daily"),
        ("交周报", "weekly"),
    ]:
        if text.startswith(prefix):
            body = text[len(prefix) :].lstrip(" ：:，,")
            if not body:
                return {"status": "needs_content", "message": "请填写一条摘要，非编码工作也可记录。"}
            if kind == "progress":
                return {"method": "progress.submit", "payload": {"task": request.task, "summary": body}}
            return {"method": "report.prepare", "payload": {"kind": kind, "body": body}, "next": f"{kind}.confirm"}
    return {"status": "needs_intent", "message": "请选择：记一条进展、补日报、交周报、我的任务、开工读档案。"}
