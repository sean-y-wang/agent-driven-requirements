"""PRD 第一批：成员配置表、首次提需问部门、附件转存、私聊查/改我的任务、延期通知。

全部为 best-effort 增量能力：表未配置、权限不足或接口失败时降级，不影响原有入池流程。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from ..config.models import FieldSpec, Tables, TableSpec
from ..writer.bitable import client_token_for
from .user_api import cell_text

log = structlog.get_logger(__name__)

REPORT_ROLES = ("管理者", "产品运营", "参与者")
REQUESTER_ROLE = "需求方"


def _cols(spec: TableSpec) -> dict[str, str]:
    return {f.key: f.field for f in spec.fields}


def _people(cell: Any) -> list[str]:
    if not isinstance(cell, list):
        return []
    out = []
    for p in cell:
        if isinstance(p, dict):
            oid = p.get("id") or p.get("open_id")
            if oid:
                out.append(oid)
    return out


def _select(cell: Any) -> list[str]:
    if cell is None:
        return []
    if isinstance(cell, list):
        return [cell_text(x) if not isinstance(x, str) else x for x in cell if x]
    return [cell_text(cell)] if cell_text(cell) else []


# ---------------------------------------------------------------- 成员配置
class MemberDirectory:
    """读「成员配置」表（缓存 ttl 秒），失败或未配置时回退 JSON 名单 / 本地部门缓存。"""

    def __init__(self, api, tables: Tables | None, store=None, *, fallback_json: str | Path | None = None,
                 ttl: float = 300.0, clock=time.time) -> None:
        self.api = api
        self.spec = tables.members if tables is not None else None
        self.store = store
        self.fallback_json = Path(fallback_json) if fallback_json else None
        self.ttl = ttl
        self.clock = clock
        self._rows: list[dict] | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.spec is not None

    def rows(self, refresh: bool = False) -> list[dict]:
        if self.spec is None:
            return []
        with self._lock:
            if not refresh and self._rows is not None and self.clock() - self._at < self.ttl:
                return self._rows
            try:
                raw = self.api.list_all_records(self.spec.app_token, self.spec.table_id)
            except Exception as e:  # permission / network → keep last good snapshot
                log.info("members.read_failed", error=str(e)[:200])
                return self._rows or []
            c = _cols(self.spec)
            rows = []
            for r in raw:
                f = r.get("fields") or {}
                for oid in _people(f.get(c.get("member", ""))):
                    rows.append({
                        "record_id": r.get("record_id", ""), "open_id": oid,
                        "name": cell_text(f.get(c.get("name", ""))),
                        "role": (_select(f.get(c.get("role", ""))) or [""])[0],
                        "dept": _select(f.get(c.get("dept", ""))),
                    })
            self._rows, self._at = rows, self.clock()
            return rows

    def find(self, open_id: str) -> dict | None:
        return next((r for r in self.rows() if r["open_id"] == open_id), None)

    def dept_of(self, open_id: str) -> list[str] | None:
        """None = 从没问过；[] = 用户跳过过（不再追问）。"""
        row = self.find(open_id) if open_id else None
        if row and row["dept"]:
            return row["dept"]
        if self.store is not None and open_id:
            return self.store.get_member_dept(open_id)
        return None

    def remember_dept(self, open_id: str, name: str, dept: list[str]) -> None:
        if self.store is not None:
            self.store.set_member_dept(open_id, dept)
        if self.spec is None or not dept:
            return
        c = _cols(self.spec)
        try:
            row = self.find(open_id)
            if row:
                self.api.update_record(self.spec.app_token, self.spec.table_id, row["record_id"],
                                       {c["dept"]: dept[0]})
            else:
                fields = {c["member"]: [{"id": open_id}], c["dept"]: dept[0]}
                if "role" in c:
                    fields[c["role"]] = REQUESTER_ROLE
                if "name" in c and name:
                    fields[c["name"]] = name
                self.api.create_record(self.spec.app_token, self.spec.table_id, fields, client_token_for(f"member:{open_id}"))
            self._rows = None
        except Exception as e:
            log.info("members.write_failed", error=str(e)[:200])

    def by_role(self, *roles: str) -> list[str]:
        return [r["open_id"] for r in self.rows() if r["role"] in roles]

    def report_members(self) -> list[dict]:
        """日报/周报成员：表中 管理者/产品运营/参与者；表为空或不可读时回退 JSON。"""
        seen, out = set(), []
        for r in self.rows():
            if r["role"] in REPORT_ROLES and r["open_id"] not in seen:
                seen.add(r["open_id"])
                out.append({"open_id": r["open_id"], "name": r["name"] or r["open_id"]})
        if out:
            return out
        if self.fallback_json and self.fallback_json.exists():
            return json.loads(self.fallback_json.read_text())
        return []


def parse_dept(text: str, spec: FieldSpec) -> list[str] | None:
    """「2」「输入法」「1、3」→ 选项列表；识别不了返回 None。"""
    opts = [o.value for o in spec.options]
    out: list[str] = []
    for tok in re.split(r"[,，、/;；\s]+", (text or "").strip()):
        if not tok:
            continue
        if tok.isdigit() and 1 <= int(tok) <= len(opts):
            hit = opts[int(tok) - 1]
        else:
            hit = next((o.value for o in spec.options
                        if tok == o.value or tok in o.synonyms or (len(tok) >= 2 and tok in o.value)), None)
        if hit is None:
            return None
        if hit not in out:
            out.append(hit)
    return out or None


def dept_question(spec: FieldSpec) -> str:
    opts = "\n".join(f"{i}. {o.value}" for i, o in enumerate(spec.options, 1))
    return (f"第一次提需，麻烦选一下你的{spec.field}（回复序号或名称，可多选，如「1、3」）：\n{opts}\n"
            "以后会自动沿用；不想填回「跳过」。")


# ---------------------------------------------------------------- 附件
_KIND = {"image": "image", "file": "file", "media": "file", "audio": "file"}


def extract_attachments(message_type: str, content: str, message_id: str) -> list[dict]:
    """Image/file keys carried by one message (image, file, or images embedded in a post)."""
    try:
        body = json.loads(content or "{}")
    except ValueError:
        return []
    out: list[dict] = []
    if message_type == "image" and body.get("image_key"):
        out.append({"message_id": message_id, "key": body["image_key"], "type": "image", "name": ""})
    elif message_type in ("file", "media", "audio") and body.get("file_key"):
        out.append({"message_id": message_id, "key": body["file_key"], "type": "file",
                    "name": body.get("file_name") or ""})
    elif message_type == "post":
        blocks = body.get("content")
        if blocks is None:  # some payloads nest by locale
            for v in body.values():
                if isinstance(v, dict) and isinstance(v.get("content"), list):
                    blocks = v["content"]
                    break
        for line in blocks or []:
            for el in line if isinstance(line, list) else []:
                if isinstance(el, dict) and el.get("tag") == "img" and el.get("image_key"):
                    out.append({"message_id": message_id, "key": el["image_key"], "type": "image", "name": ""})
                if isinstance(el, dict) and el.get("tag") == "media" and el.get("file_key"):
                    out.append({"message_id": message_id, "key": el["file_key"], "type": "file", "name": ""})
    return out


def transfer_attachments(api, app_token: str, items: list[dict], max_files: int = 10) -> list[dict]:
    """Download each message resource and upload it as Bitable media. Failures are skipped."""
    tokens: list[dict] = []
    for i, it in enumerate(items[:max_files], 1):
        try:
            data, fname = api.download_resource(it["message_id"], it["key"], _KIND.get(it["type"], "file"))
            name = it.get("name") or fname or (f"image-{i}.png" if it["type"] == "image" else f"file-{i}")
            ptype = "bitable_image" if it["type"] == "image" else "bitable_file"
            tokens.append({"file_token": api.upload_media(app_token, name, data, ptype)})
        except Exception as e:
            log.info("attachment.transfer_failed", key=str(it.get("key"))[:12], error=str(e)[:200])
    return tokens


# ---------------------------------------------------------------- 我的任务
_LIST_RE = re.compile(r"^(?:请|帮我)?(?:看看|看一下|查看|查查|查一下|查|列出|列一下)?\s*我的任务[吧呢吗？?。!！]*$")
_SET_RE = re.compile(
    r"^(?:把|将)?\s*(?:任务)?\s*(?P<target>.+?)\s*(?:的)?(?:状态)?\s*(?:改为|改成|设为|设置为|标为|标记为|更新为|变为)\s*「?(?P<status>[^」\s]+)」?\s*$"
)


@dataclass
class TaskCommand:
    kind: str  # list | set
    target: str = ""
    status: str = ""


def parse_task_command(text: str, statuses: list[str]) -> TaskCommand | None:
    t = (text or "").strip()
    if _LIST_RE.match(t):
        return TaskCommand("list")
    m = _SET_RE.match(t)
    if m and m.group("status").rstrip("。.!！") in statuses:
        return TaskCommand("set", m.group("target").strip(), m.group("status").rstrip("。.!！"))
    return None


class TaskDesk:
    PEOPLE_KEYS = ("participants", "product", "dev", "test")

    def __init__(self, api, tables: Tables, tz: str = "Asia/Shanghai", base_url: str = "") -> None:
        self.api = api
        self.spec = tables.tasks
        self.tz = tz
        self.base_url = base_url
        self.last_change: tuple[dict, str, str] | None = None

    def statuses(self) -> list[str]:
        st = next((f for f in self.spec.fields if f.key == "status"), None)
        return [o.value for o in st.options] if st else []

    def mine(self, open_id: str) -> list[dict]:
        c = _cols(self.spec)
        rows = self.api.list_all_records(self.spec.app_token, self.spec.table_id)
        out = []
        for r in rows:
            f = r.get("fields") or {}
            if any(open_id in _people(f.get(c[k])) for k in self.PEOPLE_KEYS if k in c):
                out.append(r)
        out.sort(key=lambda r: r.get("record_id", ""))
        return out

    def _line(self, i: int, r: dict) -> str:
        c, f = _cols(self.spec), r.get("fields") or {}
        title = cell_text(f.get(c["title"])) or "（无标题）"
        status = cell_text(f.get(c["status"])) or "未设置"
        due = f.get(c.get("plan_date", "")) if "plan_date" in c else None
        due_s = ""
        if isinstance(due, (int, float)):
            due_s = " ｜计划上线 " + datetime.fromtimestamp(due / 1000, UTC).astimezone(ZoneInfo(self.tz)).date().isoformat()
        return f"{i}. {title} ｜{status}{due_s}"

    def list_text(self, open_id: str) -> str:
        rows = self.mine(open_id)
        if not rows:
            return "你名下暂时没有任务（按 参与人/产品/开发/测试 匹配）。"
        lines = [f"你名下有 {len(rows)} 个任务："] + [self._line(i, r) for i, r in enumerate(rows, 1)]
        lines.append("要改状态，回复如「2 改为 开发中」或「把 任务名 改成 已上线」。可选状态：" + "/".join(self.statuses()))
        return "\n".join(lines)

    def set_status(self, open_id: str, target: str, status: str) -> str:
        self.last_change = None
        if status not in self.statuses():
            return f"没有「{status}」这个状态。可选：{'/'.join(self.statuses())}"
        rows = self.mine(open_id)
        c = _cols(self.spec)
        hits: list[dict]
        if target.isdigit():
            n = int(target)
            hits = [rows[n - 1]] if 1 <= n <= len(rows) else []
        else:
            hits = [r for r in rows if target and target in cell_text((r.get("fields") or {}).get(c["title"]))]
        if not hits:
            return "没在你名下的任务里找到它，先发「我的任务」看看序号。只能改分给自己的任务。"
        if len(hits) > 1:
            return "匹配到多个任务，请用序号：\n" + "\n".join(self._line(rows.index(r) + 1, r) for r in hits[:5])
        r = hits[0]
        old = cell_text((r.get("fields") or {}).get(c["status"])) or "未设置"
        if old == status:
            return f"「{cell_text(r['fields'].get(c['title']))}」本来就是 {status}。"
        self.api.update_record(self.spec.app_token, self.spec.table_id, r["record_id"], {c["status"]: status})
        log.info("task.status_set", record_id=r["record_id"], old=old, new=status)
        self.last_change = (r, old, status)
        return f"✅ 已把「{cell_text(r['fields'].get(c['title']))}」从 {old} 改为 {status}。"


# ---------------------------------------------------------------- 延期通知
def overdue_notices(api, tables: Tables, today: date, tz: str, done_statuses: list[str],
                    base_url: str = "") -> list[dict]:
    spec = tables.requirements
    c = _cols(spec)
    if "plan_date" not in c:
        return []
    out = []
    for r in api.list_all_records(spec.app_token, spec.table_id):
        f = r.get("fields") or {}
        due = f.get(c["plan_date"])
        if not isinstance(due, (int, float)):
            continue
        due_d = datetime.fromtimestamp(due / 1000, UTC).astimezone(ZoneInfo(tz)).date()
        status = cell_text(f.get(c.get("status", ""))) if "status" in c else ""
        if due_d >= today or status in done_statuses:
            continue
        to = _people(f.get(c.get("assignee", ""))) or _people(f.get(c.get("proposer_account", "")))
        if not to:
            continue
        title = cell_text(f.get(c["title"])) or "（无标题）"
        link = f"{base_url}/base/{spec.app_token}?table={spec.table_id}&record={r['record_id']}" if base_url else ""
        text = (f"⏰ 延期提醒：需求「{title}」计划上线日期 {due_d.isoformat()} 已过，当前状态「{status or '未设置'}」，"
                f"已超期 {(today - due_d).days} 天。请更新计划上线日期或状态。" + (f"\n{link}" if link else ""))
        out.append({"record_id": r["record_id"], "due": due_d.isoformat(), "to": to, "text": text})
    return out


def run_overdue(service, today: date | None = None) -> int:
    """One scan; each (record, 计划上线日期, 收件人) notified at most once (bot.db dedup, 180 天)."""
    cfg = service.cfg
    od = cfg.overdue_notify
    if not cfg.tables or not od.enabled:
        return 0
    today = today or datetime.fromtimestamp(service.clock(), ZoneInfo(cfg.timezone)).date()
    base = getattr(service.writer, "base_url", "")
    sent = 0
    for n in overdue_notices(service.api, cfg.tables, today, cfg.timezone, od.done_statuses, base):
        for oid in n["to"]:
            key = f"overdue:{n['record_id']}:{n['due']}:{oid}"
            if od.dry_run:
                log.info("overdue.dry_run", record_id=n["record_id"], due=n["due"], to=oid)
                continue
            if service.store.seen(key, ttl=180 * 86400):
                continue
            try:
                service.api.send_text(oid, n["text"], receive_id_type="open_id")
                sent += 1
                log.info("overdue.sent", record_id=n["record_id"], due=n["due"], to=oid)
            except Exception as e:
                log.info("overdue.send_failed", record_id=n["record_id"], error=str(e)[:200])
    return sent


def overdue_loop(service, interval: float = 1800.0) -> None:
    stop = threading.Event()
    last: date | None = None
    while True:
        try:
            cfg = service.cfg
            now = datetime.fromtimestamp(service.clock(), ZoneInfo(cfg.timezone))
            if cfg.overdue_notify.enabled and now.hour >= cfg.overdue_notify.hour and last != now.date():
                n = run_overdue(service, now.date())
                last = now.date()
                log.info("overdue.scan_done", sent=n, dry_run=cfg.overdue_notify.dry_run)
        except Exception:
            log.exception("overdue.failed")
        if stop.wait(interval):
            return
