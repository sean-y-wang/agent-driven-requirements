from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from bitable_bot.config.loader import load_config
from bitable_bot.requirements_v09 import prd2
from bitable_bot.requirements_v09.identity import progress_key
from bitable_bot.requirements_v09.reports import manager_due

TZ = "Asia/Shanghai"


@pytest.fixture(scope="module")
def tables():
    return load_config("config/roles.requirements.xfchat.example.yaml", {}).tables


def ms(d):
    return prd2.date_to_ms(d, TZ)


def task(rid, title, plan, status="开发中", owner="ou_a", **extra):
    f = {"任务标题": title, "状态": status, "计划上线日期": ms(plan) if plan else None, "开发": [{"id": owner}]}
    f.update(extra)
    return {"record_id": rid, "fields": f}


def test_detect_assignee_only_mentioned():
    m = {"@张三": "ou_z", "李四": "ou_l"}
    assert prd2.detect_assignee("这个需求交给 @张三 处理", m) == ["ou_z"]
    assert prd2.detect_assignee("执行人：李四", m) == ["ou_l"]
    assert prd2.detect_assignee("张三说这个很急", m) == []
    assert prd2.detect_assignee("交给 @张三", m, exclude=("ou_z",)) == []


def test_progress_key_tag_compat():
    old = progress_key("ou", "2026-11-03", "rec1", "Codex")
    assert progress_key("ou", "2026-11-03", "rec1", "Codex", "进展") == old
    assert progress_key("ou", "2026-11-03", "rec1", "Codex", "排期变更") != old


def test_progress_values_links_and_optional(tables):
    v = prd2.progress_values(tables, owner="ou", day="2026-11-03", task_id="recT", summary="x",
                             tag="排期变更", source="飞书私聊", req_ids=["recR"])
    assert v["task_link"] == ["recT"] and v["req_link"] == ["recR"] and v["tag"] == "排期变更"
    assert v["source"] == "飞书私聊" and "review_state" not in v


def test_parse_schedule_commands():
    today = date(2026, 11, 3)
    c = prd2.parse_schedule_command("中插 2 到 11-20 顺延 3 天", today)
    assert (c.kind, c.target, c.date, c.days) == ("insert", "2", "2026-11-20", 3)
    assert prd2.parse_schedule_command("中插 2 到 01-05", today).date == "2027-01-05"
    d = prd2.parse_schedule_command("顺延 登录改版 2 天", today)
    assert (d.kind, d.target, d.days) == ("delay", "登录改版", 2)
    assert prd2.parse_schedule_command("确认顺延", today).kind == "confirm"
    assert prd2.parse_schedule_command("今天天气不错", today) is None


def test_plan_insert_shifts_later_open_tasks(tables):
    rows = [task("r1", "A", date(2026, 11, 10)), task("r2", "B", date(2026, 11, 20)),
            task("r3", "C", date(2026, 11, 25), status="已上线"), task("r4", "D", date(2026, 11, 5))]
    plan, err = prd2.plan_insert(rows, tables, "1", date(2026, 11, 12), 3, TZ)
    assert not err
    ids = {s.record_id: s for s in plan.shifts}
    assert ids["r1"].new == date(2026, 11, 12)
    assert ids["r2"].new == date(2026, 11, 25)  # 3 workdays after Fri 11-20
    assert "r3" not in ids and "r4" not in ids
    assert "确认顺延" in prd2.plan_text(plan)


def test_plan_delay_requires_date(tables):
    plan, err = prd2.plan_delay([task("r1", "A", None)], tables, "A", 2, TZ)
    assert plan is None and "计划上线日期" in err
    plan, _ = prd2.plan_delay([task("r1", "A", date(2026, 11, 6))], tables, "A", 1, TZ)
    assert plan.shifts[0].new == date(2026, 11, 9)


def test_risk_alerts(tables):
    today = date(2026, 11, 3)
    rows = [task("rec1", "逾期", date(2026, 11, 1)), task("r2", "临近", date(2026, 11, 5)),
            task("r3", "依赖", date(2026, 11, 4), 依赖任务=["rec1"]), task("r4", "别人的", date(2026, 11, 1), owner="ou_b")]
    out = prd2.risk_alerts(None, tables, "ou_a", today, TZ, tasks=rows, progress=[])
    text = "\n".join(out)
    assert "已逾期：「逾期」" in text and "临近上线没进展：「临近」" in text and "依赖未完成：「依赖」" in text
    assert "别人的" not in text
    assert prd2.risk_section([]) == "" and "风险预警" in prd2.risk_section(out)


def test_dept_split(tables):
    rows = [{"record_id": "a", "fields": {"创建时间": ms(date(2026, 11, 2)), "提出部门": ["输入法"]}},
            {"record_id": "b", "fields": {"创建时间": ms(date(2026, 11, 3)), "提出部门": ["输入法", "研究院"]}},
            {"record_id": "c", "fields": {"创建时间": ms(date(2026, 10, 1)), "提出部门": ["研究院"]}},
            {"record_id": "d", "fields": {"创建时间": ms(date(2026, 11, 3))}}]
    counts = prd2.new_requirements_by_dept(rows, tables, date(2026, 11, 2), date(2026, 11, 8), TZ)
    assert counts == {"输入法": 2, "研究院": 1, "未填部门": 1}
    assert prd2.dept_line(counts).startswith("本周新增需求 4 条（输入法 2")
    assert prd2.dept_line({}) == "本周新增需求 0 条"


def test_manager_due():
    tz = ZoneInfo(TZ)
    mon, fri = datetime(2026, 11, 2, 9, 5, tzinfo=tz), datetime(2026, 11, 6, 14, 5, tzinfo=tz)
    assert manager_due(mon) and not manager_due(fri)
    assert manager_due(fri, "fri14") and not manager_due(mon, "fri14")
    assert manager_due(mon, "both") and manager_due(fri, "both")


def test_assign_command_and_brief_permission():
    assert prd2.parse_assign_command("分配 recABC 给 @张三") == ("recABC", "张三")
    assert prd2.parse_assign_command("分配一下") is None
    d = {"owners": {"ou_a"}, "requirements": [{"proposer": ["ou_p"]}]}
    assert prd2.can_view_brief(d, "ou_a", set()) and prd2.can_view_brief(d, "ou_p", set())
    assert prd2.can_view_brief(d, "ou_m", {"ou_m"}) and not prd2.can_view_brief(d, "ou_x", set())


def test_links_parsing():
    assert prd2.links([{"record_ids": None}]) == []
    assert prd2.links([{"record_ids": ["recA", "recB"]}, "recC"]) == ["recA", "recB", "recC"]
    assert prd2.links({"link_record_ids": ["recA"]}) == ["recA"]


def test_ops_flags_prd_example_on_and_model_defaults_safe():
    """xfchat example 按 t635u 拍板打开；未写配置时模型默认仍安全关闭。"""
    from bitable_bot.config import load_config
    from bitable_bot.config.models import AppConfig, OpsReviewConfig, ScheduleNotifyConfig, ReportsConfig, WeeklyDocConfig

    cfg = load_config(Path("config/roles.requirements.xfchat.example.yaml"), env={
        "REQUIREMENTS_APP_TOKEN": "bascnEXAMPLE",
        "REQUIREMENTS_TABLE_ID": "t1",
        "TASKS_TABLE_ID": "t2",
        "PROGRESS_TABLE_ID": "t3",
        "MODULES_TABLE_ID": "t4",
        "MEMBERS_TABLE_ID": "t5",
        "EXTERNAL_TABLE_ID": "t6",
        "PRIVATE_LLM_KEY": "x",
    })
    assert cfg.ops_review.enabled is True
    assert cfg.schedule_notify.dry_run is False
    assert cfg.overdue_notify.dry_run is False
    assert cfg.reports.risk_to_ops is True
    assert cfg.reports.manager_schedule == "fri14"
    assert cfg.weekly_doc.enabled is True
    assert cfg.roles[0].confirm_before_write is False
    # bare model defaults remain safe-off
    assert OpsReviewConfig().enabled is False
    assert ScheduleNotifyConfig().dry_run is True
    assert ReportsConfig().risk_to_ops is False
    assert WeeklyDocConfig().enabled is False

def test_refresh_open_deps_minimal_writes(tables):
    class API:
        def __init__(self):
            self.updates = []

        def list_all_records(self, *_):
            return [{"record_id": "recA", "fields": {"状态": "开发中", "未完成依赖": "0"}},
                    {"record_id": "recB", "fields": {"状态": "开发中", "依赖任务": ["recA"], "未完成依赖": "1"}},
                    {"record_id": "recC", "fields": {"状态": "开发中", "依赖任务": ["recA"]}},
                    {"record_id": "recD", "fields": {"状态": "开发中"}}]

        def update_record(self, app, table, rid, fields):
            self.updates.append((rid, fields))

    api = API()
    assert prd2.refresh_open_deps(api, tables) == 1
    assert api.updates == [("recC", {"未完成依赖": 1})]
