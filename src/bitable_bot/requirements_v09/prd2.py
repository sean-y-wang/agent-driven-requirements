"""PRD 第二批：指定执行人、改状态写进展、中插/顺延与排期变更、任务简报/档案、日报风险预警、
周报云文档与管理者部门拆分、产品运营评估卡（默认关闭）。

全部 best-effort：可选字段没在配置里声明时自动跳过；对外发消息的能力都有 dry_run 开关。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from ..config.models import Tables, TableSpec
from .identity import progress_key
from .user_api import cell_text

log = structlog.get_logger(__name__)

DONE_TASK = ("待验收", "已上线")
CLOSED_TASK = ("待验收", "已上线", "已暂停", "已关闭", "已完成")


def cols(spec: TableSpec | None) -> dict[str, str]:
    return {f.key: f.field for f in spec.fields} if spec is not None else {}


def people(cell: Any) -> list[str]:
    out = []
    for p in cell if isinstance(cell, list) else []:
        if isinstance(p, dict) and (p.get("id") or p.get("open_id")):
            out.append(p.get("id") or p.get("open_id"))
    return out


def links(cell: Any) -> list[str]:
    """Record ids from a link cell (list of ids, or [{record_ids: [...]}] / [{link_record_ids: ...}])."""
    out: list[str] = []
    if isinstance(cell, dict):
        cell = [cell]
    for x in cell if isinstance(cell, list) else []:
        if isinstance(x, str) and x.startswith("rec"):
            out.append(x)
        elif isinstance(x, dict):
            for k in ("record_ids", "link_record_ids"):
                out += [r for r in (x.get(k) or []) if isinstance(r, str)]
            if isinstance(x.get("record_id"), str):
                out.append(x["record_id"])
    return list(dict.fromkeys(out))


def ms_to_date(v: Any, tz: str) -> date | None:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, UTC).astimezone(ZoneInfo(tz)).date()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def date_to_ms(d: date, tz: str) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(tz)).timestamp() * 1000)


def add_workdays(d: date, n: int) -> date:
    step = 1 if n >= 0 else -1
    left = abs(n)
    while left:
        d += timedelta(days=step)
        if d.weekday() < 5:
            left -= 1
    return d


# ---------------------------------------------------------------- 指定执行人
_ASSIGN_RE = [
    r"(?:指定|找|交给|交由|让|由|请|麻烦|分给|派给)\s*@?\s*{n}\s*(?:来|去)?(?:处理|负责|跟进|做|看|改|修|对接|评估|排期)",
    r"(?:执行人|处理人|受理人|负责人|对接人)\s*[:：是为]?\s*@?\s*{n}",
    r"(?:指定|交给|交由|分给|派给)\s*@?\s*{n}",
    r"@?\s*{n}\s*(?:来|你来|负责|处理一下|跟进一下|帮忙处理|看一下|看下)",
]


def detect_assignee(text: str, mentioned: dict[str, str], exclude: tuple[str, ...] = ()) -> list[str]:
    """从提需文字里识别「指定执行人」：只认被 @ 的人（open_id 可靠，不按名字猜）。"""
    t = text or ""
    out: list[str] = []
    for name, oid in (mentioned or {}).items():
        if not name or not oid or oid in exclude:
            continue
        n = re.escape(name.lstrip("@"))
        if any(re.search(p.format(n=n), t) for p in _ASSIGN_RE):
            out.append(oid)
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- 进展日志写入（排期变更/状态）
def progress_values(tables: Tables, *, owner: str, day: str, task_id: str, summary: str, tag: str,
                    source: str, req_ids: list[str] | None = None, review: bool = False,
                    task_fields: dict | None = None) -> dict:
    """组装进展日志一行；未在配置里声明的可选字段自动省略。去重键 = 本人+日期+任务+来源(+标签)。"""
    c = cols(tables.progress)
    tc = cols(tables.tasks)
    tf = task_fields or {}
    v: dict[str, Any] = {
        "task": task_id, "author": [{"id": owner}], "day": day, "summary": summary[:200],
        "review": review, "sync_key": progress_key(owner, day, task_id, source, tag),
    }
    if "agent" in c:
        v["agent"] = {"飞书私聊": "其他", "飞书群": "群"}.get(source, source if source in ("Claude Code", "Codex", "Orca") else "其他")
    opt = {"tag": tag, "source": source, "task_link": [task_id] if task_id.startswith("rec") else None,
           "req_link": req_ids or None, "review_state": "待复核" if review else None}
    if "module" in c and "module" in tc and tf.get(tc["module"]):
        opt["module"] = cell_text(tf.get(tc["module"]))
    for k in ("project", "feature_module"):
        if k in c and k in tc and tf.get(tc[k]):
            opt[k] = cell_text(tf.get(tc[k]))
    for k, val in opt.items():
        if k in c and val not in (None, "", []):
            v[k] = val
    return v


def task_req_ids(tables: Tables, task_fields: dict) -> list[str]:
    tc = cols(tables.tasks)
    ids = links(task_fields.get(tc.get("req_link", ""))) if "req_link" in tc else []
    txt = cell_text(task_fields.get(tc.get("requirement", ""))) if "requirement" in tc else ""
    if not ids and txt.startswith("rec"):
        ids = [txt.strip()]
    return ids


def log_status_change(writer, tables: Tables, task: dict, old: str, new: str, operator: str, tz: str,
                      now: datetime | None = None) -> str | None:
    """改任务状态后补一行进展（标签=进展，来源=飞书私聊）；失败只记日志。"""
    from .writer import MultiTableWriter

    now = now or datetime.now(ZoneInfo(tz))
    tf = task.get("fields") or {}
    title = cell_text(tf.get(cols(tables.tasks).get("title", ""))) or task["record_id"]
    vals = progress_values(tables, owner=operator, day=now.date().isoformat(), task_id=task["record_id"],
                           summary=f"将「{title}」状态由 {old} 改为 {new}", tag="进展", source="飞书私聊",
                           req_ids=task_req_ids(tables, tf), task_fields=tf)
    try:
        return MultiTableWriter(writer, tables).upsert_progress(vals).record_id
    except Exception as e:
        log.info("task.status_progress_failed", error=str(e)[:200])
        return None


# ---------------------------------------------------------------- 中插 / 顺延
_INSERT_RE = re.compile(
    r"^中插\s*(?P<target>\S+?)\s*(?:到|在|于|上线)?\s*(?P<date>\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[-/.月]\d{1,2}日?)"
    r"(?:\s*(?:其他|其余)?\s*顺延\s*(?P<days>\d+)\s*(?:个)?(?:工作日|天))?\s*$")
_DELAY_RE = re.compile(r"^顺延\s*(?P<target>\S+?)\s*(?P<days>\d+)\s*(?:个)?(?:工作日|天)\s*$")
_CONFIRM_RE = re.compile(r"^(?:确认顺延|确认改期|确认中插)$")
_BRIEF_RE = re.compile(r"^(?:任务简报|任务档案|简报|档案)\s*(?P<target>.+?)\s*$|^(?P<target2>.+?)\s*(?:的)?(?:任务)?(?:简报|档案)$")


@dataclass
class ScheduleCommand:
    kind: str  # insert | delay | confirm | brief
    target: str = ""
    date: str = ""
    days: int = 0


def parse_schedule_command(text: str, today: date) -> ScheduleCommand | None:
    t = (text or "").strip()
    if _CONFIRM_RE.match(t):
        return ScheduleCommand("confirm")
    m = _INSERT_RE.match(t)
    if m:
        ds = m.group("date")
        if re.match(r"^\d{4}-", ds):
            y, mo, d = (int(x) for x in ds.split("-"))
        else:
            mo, d = (int(x) for x in re.split(r"[-/.月]", ds.rstrip("日")))
            y = today.year if (mo, d) >= (today.month, today.day) else today.year + 1
        try:
            iso = date(y, mo, d).isoformat()
        except ValueError:
            return None
        return ScheduleCommand("insert", m.group("target"), iso, int(m.group("days") or 0))
    m = _DELAY_RE.match(t)
    if m:
        return ScheduleCommand("delay", m.group("target"), days=int(m.group("days")))
    m = _BRIEF_RE.match(t)
    if m:
        tgt = (m.group("target") or m.group("target2") or "").strip("「」 ")
        if tgt and tgt not in ("我的", "我"):
            return ScheduleCommand("brief", tgt)
    return None


@dataclass
class Shift:
    record_id: str
    title: str
    old: date | None
    new: date
    reason: str


@dataclass
class SchedulePlan:
    owner: str
    shifts: list[Shift] = field(default_factory=list)
    created: float = 0.0


def _resolve(rows: list[dict], target: str, title_col: str) -> list[dict]:
    if target.isdigit():
        n = int(target)
        return [rows[n - 1]] if 1 <= n <= len(rows) else []
    exact = [r for r in rows if cell_text((r.get("fields") or {}).get(title_col)) == target]
    return exact or [r for r in rows if target in cell_text((r.get("fields") or {}).get(title_col))]


def plan_insert(rows: list[dict], tables: Tables, target: str, new_date: date, days: int, tz: str
                ) -> tuple[SchedulePlan | None, str]:
    """rows = 本人任务（同 TaskDesk.mine 顺序）。中插 target 到 new_date；本人其他未完成、计划上线日期
    ≥ new_date 的任务顺延 days 个工作日（未给 days 时默认按中插任务的「预计开发周期」，再缺省 3）。"""
    c = cols(tables.tasks)
    hits = _resolve(rows, target, c["title"])
    if len(hits) != 1:
        return None, "没唯一匹配到这个任务，先发「我的任务」看序号，再发如「中插 2 到 11-20 顺延 3 天」。"
    t = hits[0]
    tf = t.get("fields") or {}
    if not days:
        est = tf.get(c.get("estimate", "")) if "estimate" in c else None
        days = int(est) if isinstance(est, (int, float)) and est > 0 else 3
    title = cell_text(tf.get(c["title"]))
    plan = SchedulePlan(owner="")
    plan.shifts.append(Shift(t["record_id"], title, ms_to_date(tf.get(c.get("plan_date", "")), tz), new_date,
                             f"中插，计划上线改为 {new_date}"))
    for r in rows:
        if r is t:
            continue
        f = r.get("fields") or {}
        st = cell_text(f.get(c.get("status", "")))
        d = ms_to_date(f.get(c.get("plan_date", "")), tz)
        if st in CLOSED_TASK or d is None or d < new_date:
            continue
        plan.shifts.append(Shift(r["record_id"], cell_text(f.get(c["title"])), d, add_workdays(d, days),
                                 f"因「{title}」中插顺延 {days} 个工作日"))
    return plan, ""


def plan_delay(rows: list[dict], tables: Tables, target: str, days: int, tz: str) -> tuple[SchedulePlan | None, str]:
    c = cols(tables.tasks)
    hits = _resolve(rows, target, c["title"])
    if len(hits) != 1:
        return None, "没唯一匹配到这个任务，先发「我的任务」看序号。"
    f = hits[0].get("fields") or {}
    d = ms_to_date(f.get(c.get("plan_date", "")), tz)
    if d is None:
        return None, "这个任务还没有计划上线日期，没法顺延；请先在表里填上。"
    plan = SchedulePlan(owner="")
    plan.shifts.append(Shift(hits[0]["record_id"], cell_text(f.get(c["title"])), d, add_workdays(d, days),
                             f"顺延 {days} 个工作日"))
    return plan, ""


def plan_text(plan: SchedulePlan) -> str:
    lines = ["排期变更预览（还没改表）："]
    for s in plan.shifts:
        lines.append(f"- {s.title}：{s.old or '未排'} → {s.new}（{s.reason}）")
    lines.append("确认无误回复「确认顺延」；10 分钟内有效，不回就不改。")
    return "\n".join(lines)


def downstream(tasks: list[dict], tables: Tables, ids: set[str]) -> list[dict]:
    c = cols(tables.tasks)
    if "deps" not in c:
        return []
    return [r for r in tasks if ids & set(links((r.get("fields") or {}).get(c["deps"]))) and r["record_id"] not in ids]


def apply_plan(service, plan: SchedulePlan, operator: str) -> str:
    """改计划上线日期 → 每个任务记一条「排期变更」→ 通知被推迟需求的提出人与下游执行人（受 dry_run 控制）。"""
    from .writer import MultiTableWriter

    cfg, api, tables = service.cfg, service.api, service.cfg.tables
    tz = cfg.timezone
    c, rc = cols(tables.tasks), cols(tables.requirements)
    now = datetime.fromtimestamp(service.clock(), ZoneInfo(tz))
    all_tasks = api.list_all_records(tables.tasks.app_token, tables.tasks.table_id)
    by_id = {r["record_id"]: r for r in all_tasks}
    reqs = None
    notices: list[tuple[str, str]] = []
    changed = 0
    for s in plan.shifts:
        api.update_record(tables.tasks.app_token, tables.tasks.table_id, s.record_id, {c["plan_date"]: date_to_ms(s.new, tz)})
        changed += 1
        tf = (by_id.get(s.record_id) or {}).get("fields") or {}
        vals = progress_values(tables, owner=operator, day=now.date().isoformat(), task_id=s.record_id,
                               summary=f"排期变更：「{s.title}」计划上线 {s.old or '未排'} → {s.new}，{s.reason}",
                               tag="排期变更", source="飞书私聊", req_ids=task_req_ids(tables, tf), task_fields=tf)
        try:
            MultiTableWriter(service.writer, tables).upsert_progress(vals)
        except Exception as e:
            log.info("schedule.progress_failed", error=str(e)[:200])
        if s.old is not None and s.new <= s.old:
            continue  # 提前不算延期
        msg = (f"⏰ 排期变更：「{s.title}」计划上线 {s.old or '未排'} → {s.new}。原因：{s.reason}。")
        if reqs is None:
            reqs = {r["record_id"]: r for r in api.list_all_records(tables.requirements.app_token, tables.requirements.table_id)}
        for rid in task_req_ids(tables, tf):
            rf = (reqs.get(rid) or {}).get("fields") or {}
            for oid in people(rf.get(rc.get("proposer_account", ""))):
                if oid != operator:
                    notices.append((oid, "你提的需求受影响 —— " + msg))
        for d in downstream(all_tasks, tables, {s.record_id}):
            df = d.get("fields") or {}
            for k in ("dev", "participants"):
                for oid in people(df.get(c.get(k, ""))):
                    if oid != operator:
                        notices.append((oid, f"你的任务「{cell_text(df.get(c['title']))}」依赖它 —— " + msg))
    notices = list(dict.fromkeys(notices))
    sent = 0
    for oid, text in notices:
        if cfg.schedule_notify.dry_run:
            log.info("schedule.notify_dry_run", to=oid)
            continue
        try:
            api.send_text(oid, text, receive_id_type="open_id")
            sent += 1
        except Exception as e:
            log.info("schedule.notify_failed", error=str(e)[:200])
    tail = (f"；通知 {len(notices)} 人" + ("（试运行，未实际发送）" if cfg.schedule_notify.dry_run else f"，已发 {sent} 条")) if notices else ""
    return f"✅ 已改 {changed} 个任务的计划上线日期，进展日志各记一条「排期变更」{tail}。"


# ---------------------------------------------------------------- 任务档案 / 简报
def dossier(api, tables: Tables, task_id: str, tz: str, *, tasks: list[dict] | None = None,
            base_url: str = "", timeline: int = 10) -> dict:
    """任务档案：目标、来源需求和原始链接、依赖/被依赖、外部依赖、进展时间线、未解除风险。不另建表。"""
    c, rc, pc = cols(tables.tasks), cols(tables.requirements), cols(tables.progress)
    tasks = tasks if tasks is not None else api.list_all_records(tables.tasks.app_token, tables.tasks.table_id)
    by_id = {r["record_id"]: r for r in tasks}
    t = by_id.get(task_id)
    if t is None:
        raise ValueError("任务不存在")
    tf = t.get("fields") or {}

    def tline(r):
        f = r.get("fields") or {}
        return {"record_id": r["record_id"], "title": cell_text(f.get(c["title"])),
                "status": cell_text(f.get(c.get("status", ""))),
                "plan_date": (ms_to_date(f.get(c.get("plan_date", "")), tz) or "") and ms_to_date(f.get(c.get("plan_date", "")), tz).isoformat()}

    req_ids = task_req_ids(tables, tf)
    reqs = []
    if req_ids:
        allr = {r["record_id"]: r for r in api.list_all_records(tables.requirements.app_token, tables.requirements.table_id)}
        for rid in req_ids:
            rf = (allr.get(rid) or {}).get("fields") or {}
            link = rf.get(rc.get("message_link", ""))
            reqs.append({"record_id": rid, "title": cell_text(rf.get(rc["title"])),
                         "desc": cell_text(rf.get(rc.get("desc", "")))[:300],
                         "status": cell_text(rf.get(rc.get("status", ""))),
                         "proposer": people(rf.get(rc.get("proposer_account", ""))),
                         "link": (link.get("link") if isinstance(link, dict) else cell_text(link)) or ""})
    deps = [tline(by_id[i]) for i in links(tf.get(c.get("deps", ""))) if i in by_id] if "deps" in c else []
    waiting = [tline(r) for r in downstream(tasks, tables, {task_id})]
    prog = []
    for r in api.list_all_records(tables.progress.app_token, tables.progress.table_id):
        f = r.get("fields") or {}
        if task_id in links(f.get(pc.get("task_link", ""))) or cell_text(f.get(pc.get("task", ""))) == task_id:
            prog.append({"day": cell_text(f.get(pc.get("day", ""))), "tag": cell_text(f.get(pc.get("tag", ""))) or "进展",
                         "summary": cell_text(f.get(pc.get("summary", ""))), "risk": cell_text(f.get(pc.get("risk", ""))),
                         "who": people(f.get(pc.get("author", "")))})
    prog.sort(key=lambda p: p["day"])
    risks: list[str] = []
    for p in prog:
        if p["tag"] == "风险" or p["risk"]:
            risks.append(p["risk"] or p["summary"])
        elif p["tag"] == "风险解除" and risks:
            risks.pop(0)
    ext = []
    if "ext_link" in c and tables.external is not None:
        ec = cols(tables.external)
        alle = {r["record_id"]: r for r in api.list_all_records(tables.external.app_token, tables.external.table_id)}
        for i in links(tf.get(c["ext_link"])):
            ef = (alle.get(i) or {}).get("fields") or {}
            ext.append({"title": cell_text(ef.get(ec.get("title", ""))), "status": cell_text(ef.get(ec.get("status", "")))})
    open_deps = [d for d in deps if d["status"] not in DONE_TASK] + [e for e in ext if e["status"] != "已交付"]
    owners = sorted({o for k in ("participants", "product", "dev", "test") for o in people(tf.get(c.get(k, "")))})
    return {"task": tline(t), "goal": cell_text(tf.get(c.get("goal", ""))), "requirements": reqs, "deps": deps,
            "external": ext, "waiting_on_me": waiting, "open_deps": len(open_deps), "timeline": prog[-timeline:],
            "risks": risks, "owners": owners,
            "url": f"{base_url}/base/{tables.tasks.app_token}?table={tables.tasks.table_id}&record={task_id}" if base_url else ""}


def brief_text(d: dict) -> str:
    t = d["task"]
    lines = [f"📋 任务简报：{t['title']}（{t['status'] or '未设置'}" + (f"，计划上线 {t['plan_date']}" if t["plan_date"] else "") + "）",
             f"目标：{d['goal'] or '（未填任务目标）'}"]
    for r in d["requirements"]:
        lines.append(f"来源需求：{r['title']}（{r['status']}）" + (f"\n原始群聊：{r['link']}" if r["link"] else ""))
    if d["deps"] or d["external"]:
        dep = [f"{x['title']}[{x['status'] or '未设置'}]" for x in d["deps"]] + [f"外部·{x['title']}[{x['status'] or '未设置'}]" for x in d["external"]]
        lines.append(f"依赖（未完成 {d['open_deps']}）：" + "；".join(dep))
    if d["waiting_on_me"]:
        lines.append(f"有 {len(d['waiting_on_me'])} 个任务在等它：" + "；".join(x["title"] for x in d["waiting_on_me"][:5]))
    if d["timeline"]:
        lines.append("最近进展：")
        lines += [f"- {p['day']} [{p['tag']}] {p['summary'][:60]}" for p in d["timeline"][-5:]]
    else:
        lines.append("最近进展：暂无")
    lines.append("未解除风险：" + ("；".join(x[:60] for x in d["risks"]) if d["risks"] else "无"))
    if d["url"]:
        lines.append(d["url"])
    return "\n".join(lines)


def can_view_brief(d: dict, open_id: str, privileged: set[str]) -> bool:
    if open_id in privileged or open_id in d["owners"]:
        return True
    return any(open_id in r["proposer"] for r in d["requirements"])


# ---------------------------------------------------------------- 未完成依赖计数 / 换执行人发简报
def refresh_open_deps(api, tables: Tables) -> int:
    c = cols(tables.tasks)
    if "deps" not in c or "open_deps" not in c:
        return 0
    rows = api.list_all_records(tables.tasks.app_token, tables.tasks.table_id)
    status = {r["record_id"]: cell_text((r.get("fields") or {}).get(c.get("status", ""))) for r in rows}
    n = 0
    for r in rows:
        f = r.get("fields") or {}
        cnt = sum(1 for i in links(f.get(c["deps"])) if status.get(i, "") not in DONE_TASK)
        cur = f.get(c["open_deps"])
        try:
            same = cur is not None and cur != "" and float(cur) == cnt
        except (TypeError, ValueError):
            same = False
        if not same and not (cnt == 0 and cur in (None, "")):  # 空且无依赖：不写，避免无谓修改触发自动化
            api.update_record(tables.tasks.app_token, tables.tasks.table_id, r["record_id"], {c["open_deps"]: cnt})
            n += 1
    return n


def assignment_briefs(service) -> int:
    """开发/参与人里新出现的人 → 私聊发任务简报（PRD：换执行人时发给新执行人）。首次运行只建基线不发。"""
    cfg, api, tables, store = service.cfg, service.api, service.cfg.tables, service.store
    c = cols(tables.tasks)
    rows = api.list_all_records(tables.tasks.app_token, tables.tasks.table_id)
    baseline = not store.seen("assign:baseline:v1", ttl=10 * 365 * 86400)
    sent = 0
    for r in rows:
        f = r.get("fields") or {}
        for oid in sorted({o for k in ("dev", "participants") for o in people(f.get(c.get(k, "")))}):
            if store.seen(f"assign:{r['record_id']}:{oid}", ttl=365 * 86400) or baseline:
                continue
            if cfg.schedule_notify.dry_run:
                log.info("assign.brief_dry_run", record_id=r["record_id"], to=oid)
                continue
            try:
                d = dossier(api, tables, r["record_id"], cfg.timezone, tasks=rows, base_url=getattr(service.writer, "base_url", ""))
                api.send_text(oid, "你被分配了一个任务 👇\n" + brief_text(d), receive_id_type="open_id")
                sent += 1
            except Exception as e:
                log.info("assign.brief_failed", error=str(e)[:200])
    return sent


def maintenance_loop(service, interval: float = 1800.0) -> None:
    stop = threading.Event()
    while True:
        try:
            if service.cfg.tables is not None:
                n = refresh_open_deps(service.api, service.cfg.tables)
                b = assignment_briefs(service) if service.cfg.schedule_notify.assign_brief else 0
                log.info("maintenance.done", open_deps_updated=n, briefs=b)
        except Exception:
            log.exception("maintenance.failed")
        if stop.wait(interval):
            return


# ---------------------------------------------------------------- 日报风险预警
def risk_alerts(api, tables: Tables, owner: str, today: date, tz: str, *, near_days: int = 3,
                stale_days: int = 3, tasks: list[dict] | None = None, progress: list[dict] | None = None) -> list[str]:
    """本人参与任务的风险：已逾期、临近上线没进展、依赖未完成（内部依赖 + 外部依赖）。"""
    c, pc = cols(tables.tasks), cols(tables.progress)
    tasks = tasks if tasks is not None else api.list_all_records(tables.tasks.app_token, tables.tasks.table_id)
    progress = progress if progress is not None else api.list_all_records(tables.progress.app_token, tables.progress.table_id)
    status = {r["record_id"]: cell_text((r.get("fields") or {}).get(c.get("status", ""))) for r in tasks}
    last: dict[str, date] = {}
    for p in progress:
        f = p.get("fields") or {}
        try:
            d = date.fromisoformat(cell_text(f.get(pc.get("day", "")))[:10])
        except ValueError:
            continue
        for tid in set(links(f.get(pc.get("task_link", "")))) | {cell_text(f.get(pc.get("task", "")))}:
            if tid and (tid not in last or d > last[tid]):
                last[tid] = d
    out = []
    for r in tasks:
        f = r.get("fields") or {}
        if owner not in {o for k in ("participants", "product", "dev", "test") for o in people(f.get(c.get(k, "")))}:
            continue
        st = status[r["record_id"]]
        if st in CLOSED_TASK:
            continue
        title = cell_text(f.get(c["title"])) or r["record_id"]
        due = ms_to_date(f.get(c.get("plan_date", "")), tz)
        start = ms_to_date(f.get(c.get("dev_date", "")), tz) if "dev_date" in c else None
        if due and due < today:
            out.append(f"已逾期：「{title}」计划上线 {due}，超期 {(today - due).days} 天（{st or '未设置'}）")
        elif due and (due - today).days <= near_days and (r["record_id"] not in last or (today - last[r["record_id"]]).days >= stale_days):
            out.append(f"临近上线没进展：「{title}」{due} 上线，最近进展 {last.get(r['record_id'], '无')}")
        open_deps = [i for i in links(f.get(c.get("deps", ""))) if status.get(i, "") not in DONE_TASK] if "deps" in c else []
        n_ext = len(links(f.get(c.get("ext_link", "")))) if "ext_link" in c else 0
        anchor = start or due
        if (open_deps or n_ext) and (anchor is None or (anchor - today).days <= near_days):
            if open_deps:
                out.append(f"依赖未完成：「{title}」还有 {len(open_deps)} 个依赖任务没完成")
            if n_ext:
                out.append(f"外部依赖：「{title}」关联 {n_ext} 个外部依赖，开工前请确认已交付")
        if st == "已暂停":
            out.append(f"已暂停：「{title}」")
    return out


def risk_section(lines: list[str]) -> str:
    return ("\n风险预警：\n" + "\n".join(f"- {x}" for x in lines)) if lines else ""


# ---------------------------------------------------------------- 周报：部门拆分 / 云文档
def new_requirements_by_dept(rows: list[dict], tables: Tables, start: date, end: date, tz: str) -> dict[str, int]:
    rc = cols(tables.requirements)
    out: dict[str, int] = {}
    for r in rows:
        f = r.get("fields") or {}
        d = ms_to_date(f.get(rc.get("created_at", "")) or f.get(rc.get("submitted_at", "")), tz)
        if d is None or not (start <= d <= end):
            continue
        depts = f.get(rc.get("dept", "")) or ["未填部门"]
        for x in depts if isinstance(depts, list) else [depts]:
            out[cell_text(x) or "未填部门"] = out.get(cell_text(x) or "未填部门", 0) + 1
    return out


def dept_line(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    if not total:
        return "本周新增需求 0 条"
    return f"本周新增需求 {total} 条（" + "，".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) + "）"


class WeeklyDoc:
    """把已确认的个人周报写进当周云文档（每周一篇、每人一个小节）。

    xfchat 应用身份列不到任何知识库空间（wiki/v2/spaces 为空），且 PRD 里知识库节点地址待补充，
    所以默认写云文档（folder_token 为空 = 应用根目录）；配置 wiki_space_id + wiki_parent_node 时尝试挂到知识库。
    """

    def __init__(self, http, *, folder_token: str = "", wiki_space_id: str = "", wiki_parent_node: str = "",
                 title_fmt: str = "{year} 第 {week} 周周报", base_url: str = "") -> None:
        self.http = http  # callable(method, path, body) -> dict(data)
        self.folder_token = folder_token
        self.wiki_space_id = wiki_space_id
        self.wiki_parent_node = wiki_parent_node
        self.title_fmt = title_fmt
        self.base_url = base_url

    def title(self, day: date) -> str:
        y, w, _ = day.isocalendar()
        return self.title_fmt.format(year=y, week=w)

    def find(self, title: str) -> str | None:
        q = "/drive/v1/files?page_size=200" + (f"&folder_token={self.folder_token}" if self.folder_token else "")
        for f in (self.http("GET", q, None).get("files") or []):
            if f.get("type") == "docx" and f.get("name") == title:
                return f["token"]
        return None

    def ensure(self, day: date) -> str:
        title = self.title(day)
        tok = self.find(title)
        if tok:
            return tok
        body = {"title": title}
        if self.folder_token:
            body["folder_token"] = self.folder_token
        tok = self.http("POST", "/docx/v1/documents", body)["document"]["document_id"]
        if self.wiki_space_id and self.wiki_parent_node:
            try:
                self.http("POST", f"/wiki/v2/spaces/{self.wiki_space_id}/nodes/move_docs_to_wiki",
                          {"parent_wiki_token": self.wiki_parent_node, "obj_type": "docx", "obj_token": tok})
            except Exception as e:
                log.info("weekly_doc.wiki_move_failed", error=str(e)[:200])
        return tok

    def append_section(self, day: date, name: str, body: str) -> str:
        tok = self.ensure(day)
        children = [{"block_type": 4, "heading2": {"elements": [{"text_run": {"content": name}}]}}]
        for line in (body or "").splitlines():
            if line.strip():
                children.append({"block_type": 2, "text": {"elements": [{"text_run": {"content": line[:2000]}}]}})
        for i in range(0, len(children), 50):
            self.http("POST", f"/docx/v1/documents/{tok}/blocks/{tok}/children", {"children": children[i:i + 50], "index": -1})
        return f"{self.base_url}/docx/{tok}" if self.base_url else tok


def tenant_http(app_id: str, app_secret: str, base: str):
    import json as _json
    import urllib.request

    cache: dict[str, Any] = {}

    def token() -> str:
        if cache.get("exp", 0) < time.time():
            r = urllib.request.Request(base + "/open-apis/auth/v3/tenant_access_token/internal",
                                       data=_json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
                                       headers={"Content-Type": "application/json"})
            d = _json.load(urllib.request.urlopen(r, timeout=20))
            cache.update(tok=d["tenant_access_token"], exp=time.time() + d.get("expire", 7200) - 120)
        return cache["tok"]

    def http(method: str, path: str, body: dict | None) -> dict:
        r = urllib.request.Request(base + "/open-apis" + path, method=method,
                                   data=_json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json; charset=utf-8",
                                            "Authorization": "Bearer " + token(), "User-Agent": "Mozilla/5.0 req-bot"})
        d = _json.load(urllib.request.urlopen(r, timeout=30))
        if d.get("code") != 0:
            raise RuntimeError(f"{d.get('code')} {d.get('msg')}")
        return d.get("data") or {}

    return http


# ---------------------------------------------------------------- 产品运营评估卡（默认关闭）
def ops_review_card(title: str, desc: str, proposer: str, dept: str, record_id: str, url: str) -> dict:
    v = {"kind": "ops_review", "record_id": record_id}
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "需求待评估"}, "template": "orange"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**\n{desc[:300]}\n提出人：{proposer}　部门：{dept or '未填'}"}},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "转需求（待排期）"}, "type": "primary", "value": {**v, "op": "accept"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "标 Bug"}, "value": {**v, "op": "bug"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "关闭"}, "type": "danger", "value": {**v, "op": "close"}},
            ]},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"分配执行人：私聊我「分配 {record_id} 给 @某人」，会建好研发任务并发简报。" + (f"\n[打开记录]({url})" if url else "")}},
        ],
    }


OPS_OPS = {"accept": {"status": "待排期"}, "bug": {"status": "待开发", "type": "BugFix"}, "close": {"status": "已关闭"}}
_ASSIGN_CMD = re.compile(r"^分配\s*(?P<rid>rec\w+)\s*给\s*@?(?P<who>.+?)\s*$")


def parse_assign_command(text: str) -> tuple[str, str] | None:
    m = _ASSIGN_CMD.match((text or "").strip())
    return (m.group("rid"), m.group("who")) if m else None


def create_task_for_requirement(api, writer, tables: Tables, req_id: str, assignee: str) -> str:
    """分配执行人：建研发任务（任务目标取需求描述），挂来源业务需求，写需求受理人。"""
    rc, c = cols(tables.requirements), cols(tables.tasks)
    rows = {r["record_id"]: r for r in api.list_all_records(tables.requirements.app_token, tables.requirements.table_id)}
    rf = (rows.get(req_id) or {}).get("fields")
    if rf is None:
        raise ValueError("需求不存在")
    fields: dict[str, Any] = {c["title"]: cell_text(rf.get(rc["title"]))}
    if "goal" in c:
        fields[c["goal"]] = (cell_text(rf.get(rc.get("desc", ""))) or cell_text(rf.get(rc["title"])))[:500]
    if "req_link" in c:
        fields[c["req_link"]] = [req_id]
    if "requirement" in c:
        fields[c["requirement"]] = req_id
    for k in ("dev", "participants"):
        if k in c:
            fields[c[k]] = [{"id": assignee}]
    if "status" in c:
        fields[c["status"]] = "待排期"
    from ..writer.bitable import client_token_for

    tid = api.create_record(tables.tasks.app_token, tables.tasks.table_id, fields, client_token_for(f"assign:{req_id}:{assignee}"))
    if "assignee" in rc:
        api.update_record(tables.requirements.app_token, tables.requirements.table_id, req_id, {rc["assignee"]: [{"id": assignee}]})
    return tid
