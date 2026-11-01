import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import pytest

from bitable_bot.config import load_config
from bitable_bot.personal_agent import PersonalAgent
from bitable_bot.personal_agent.cards import handle_report_action
from bitable_bot.personal_agent.cli import FixtureAPI, main
from bitable_bot.requirements_v09.identity import progress_key
from bitable_bot.requirements_v09.progress import sync
from bitable_bot.requirements_v09.reports import (
    Member,
    ReportInput,
    ReportStore,
    entries_from_records,
    scheduled_payloads,
)
from tests.unit.test_v09_progress import project  # noqa: F401
from tests.unit.test_v09_tables import CONFIG

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/v10/personal.json"
NOW = datetime.fromisoformat("2026-11-06T18:00:00+08:00")


class MemoryAPI(FixtureAPI):
    def __init__(self, cfg):
        super().__init__(json.loads(FIXTURE.read_text()), cfg.tables)
        self.created = self.updated = 0

    def create_record(self, app, table, fields, token):
        self.created += 1
        rid = f"recCreated{self.created}"
        self.list_all_records(app, table).append({"record_id": rid, "fields": copy.deepcopy(fields)})
        return rid

    def update_record(self, app, table, rid, fields):
        self.updated += 1
        row = next(r for r in self.list_all_records(app, table) if r["record_id"] == rid)
        row["fields"].update(fields)


@pytest.fixture
def pa(tmp_path, monkeypatch):
    monkeypatch.setenv("PROGRESS_LOCK_DIR", str(tmp_path / "locks"))
    cfg = load_config(CONFIG)
    api = MemoryAPI(cfg)
    store = ReportStore(tmp_path / "reports.db")
    client = PersonalAgent(cfg, api, store, clock=lambda: NOW)
    yield client
    store.close()


def prepare(pa, kind="daily"):
    return pa.call("report.prepare", {"kind": kind})


def action(draft, **kwargs):
    return {"report_id": draft["id"], "revision": draft["revision"], **kwargs}


def report_input(pa):
    return ReportInput(
        members=[Member(open_id="ou_alice", name="小王"), Member(open_id="ou_bob", name="小李")],
        progress=entries_from_records(pa.records("progress"), pa.cfg.tables.progress),
    )


def test_s1_one_report_items_edit_confirm_and_snapshot(pa):
    draft = prepare(pa)
    assert len(draft["items"]) == 1
    assert "群进展" not in draft["body"] and "未确认秘密草稿" not in draft["body"]
    assert [b["value"]["action"] for b in draft["card"]["elements"][-1]["actions"]] == ["confirm", "edit", "void"]
    edited = pa.call("report.action", action(draft, action="edit", edits={"0": "已完成接口与回归"}))
    assert "已完成接口与回归" in edited["body"]
    assert prepare(pa)["body"] == edited["body"]  # scheduler cannot clobber a user's edit
    with pytest.raises(ValueError, match="版本"):
        pa.call("daily.confirm", action(draft))
    result = pa.call("daily.confirm", action(edited))
    assert result["status"] == "confirmed"
    with pytest.raises(ValueError, match="已处理"):
        pa.call("report.action", action(edited, action="void"))
    assert prepare(pa)["status"] == "confirmed"


def test_s1_empty_manual_and_void_terminal(pa):
    pa.api.data["progress"] = []
    draft = prepare(pa)
    assert draft["empty"] and "无编码/请手填" in draft["body"]
    edited = pa.call("report.action", action(draft, action="edit", body="无编码，完成用户访谈"))
    assert pa.call("daily.confirm", action(edited))["status"] == "confirmed"
    weekly = prepare(pa, "weekly")
    assert pa.call("report.action", action(weekly, action="void"))["status"] == "void"
    assert prepare(pa, "weekly")["status"] == "void"
    assert not [c for c in pa.call("inbox")["confirmations"] if c["kind"] == "weekly"]


def test_s1_expiry_redraft_restart_and_wrong_owner(pa, tmp_path):
    draft = prepare(pa)
    pa.api.data["user"] = "ou_bob"
    with pytest.raises(ValueError, match="非本人"):
        pa.call("report.action", action(draft, action="edit", body="越权"))
    pa.api.data["user"] = "ou_alice"
    pa.clock = lambda: datetime.fromisoformat("2026-11-09T09:00:00+08:00")
    with pytest.raises(ValueError, match="过期"):
        pa.call("daily.confirm", action(draft))
    new = prepare(pa)
    assert new["redrafted"] and pa.reports.get(draft["id"])["status"] == "superseded"
    other = ReportStore(tmp_path / "reports.db")
    try:
        assert other.get(new["id"])["revision"] == new["revision"]
    finally:
        other.close()


def test_s2_shared_key_pa_and_skill_upsert_both_directions(pa, project):  # noqa: F811
    repo, mapping = project
    args = dict(repo=repo, mapping=mapping, agent="Codex", day="2026-11-06", task_id="recTask")
    skill = sync(pa.cfg, pa.api, **args)
    active = pa.call("progress.submit", {"task": "recTask", "source": "Codex", "summary": "人工更正"})
    assert active["record_id"] == skill["record_id"] and active["updated"]
    assert sync(pa.cfg, pa.api, **args)["record_id"] == skill["record_id"]
    assert pa.api.created == 1 and pa.api.updated == 2
    assert active["sync_key"] == progress_key("ou_alice", "2026-11-06", "recTask", "Codex")
    pa.call("progress.submit", {"task": "recTask", "source": "Orca", "summary": "另一来源"})
    pa.call("progress.submit", {"task": "recTask", "source": "Codex", "day": "2026-11-05", "summary": "另一天"})
    assert pa.api.created == 3


def test_s2_unmapped_skill_no_write(pa, project):  # noqa: F811
    repo, mapping = project
    mapping.write_text("{}")
    with pytest.raises(ValueError, match="映射"):
        sync(pa.cfg, pa.api, repo=repo, mapping=mapping, agent="Codex", day="2026-11-06")
    assert pa.api.created == 0


def test_s2_concurrent_submit_serializes_read_then_upsert(pa):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda i: pa.call("progress.submit", {"task": "recTask", "summary": f"进展 {i}"}), range(8))
        )
    assert len({r["record_id"] for r in results}) == 1
    assert pa.api.created == 1 and pa.api.updated == 7


def test_s3_missing_task_no_candidate_write_and_auth(pa):
    result = pa.call("progress.submit", {"summary": "完成讨论"})
    assert result["status"] == "needs_task" and not result["candidate"]["persisted"]
    assert len(result["tasks"]) == 1 and pa.api.created == 0
    for payload in [
        {"task": "recOther", "summary": "越权"},
        {"task": "recTask", "summary": "伪造", "owner": "ou_bob"},
        {"task": "recTask", "summary": "伪造群来源", "source": "群"},
        {"task": "recTask", "summary": "  "},
        {"task": "recTask", "summary": "未来", "day": "2027-01-01"},
    ]:
        with pytest.raises(ValueError):
            pa.call("progress.submit", payload)
    assert pa.api.created == 0


@pytest.mark.parametrize("method", ["daily.confirm", "weekly.confirm"])
def test_s3_active_report_submit_and_confirm(pa, method):
    result = pa.call(method, {"body": "补交：2026-11-03 完成需求分析；风险：无"})
    assert result["status"] == "confirmed" and "补交" in result["body"]
    assert not pa.api.created


def test_s3_report_kind_and_summary_mismatch_rejected(pa):
    draft = prepare(pa)
    with pytest.raises(ValueError, match="类型"):
        pa.call("weekly.confirm", action(draft))
    pa.call("report.action", action(draft, action="edit", body="修改稿"))
    with pytest.raises(ValueError, match="不同摘要"):
        pa.call("daily.confirm", {"body": "另一个摘要"})
    assert pa.reports.get(draft["id"])["status"] == "pending"


@pytest.mark.parametrize(
    "text,method",
    [
        ("帮我记一条进展：完成讨论", "progress.submit"),
        ("补日报：完成会议", "report.prepare"),
        ("交周报：全部完成", "report.prepare"),
        ("我的任务", "tasks.list"),
        ("开工读档案", "task.open"),
    ],
)
def test_s3_natural_language_is_a_tool_proposal_not_implicit_confirm(pa, text, method):
    result = pa.call("intent.route", {"text": text, "task": "recTask"})
    assert result["method"] == method
    assert not pa.api.created
    assert not pa.reports.db.execute("SELECT * FROM report_drafts").fetchall()


def test_s4_related_requirements_and_dossier_are_read_only(pa):
    assert len(pa.call("requirements.related")["requirements"]) == 1
    dossier = pa.call("task.open", {"task": "recTask"})
    assert dossier["read_only"] and dossier["requirements"][0]["record_id"] == "recReq"
    assert dossier["modules"][0]["repository"]
    assert len(dossier["progress"]) == 2  # group context is readable, never a PA confirmation
    assert "群进展" not in prepare(pa, "weekly")["body"]
    with pytest.raises(ValueError, match="本人"):
        pa.call("task.open", {"task": "recOther"})
    assert pa.api.created == pa.api.updated == 0


def test_s5_manager_monday_uses_frozen_confirmed_body_and_risks(pa):
    source = report_input(pa)
    before = scheduled_payloads(source, NOW, pa.reports)["manager"]
    assert before["missing"] == ["小王", "小李"] and before["risks"] == []
    assert "完成主动入口" not in before["body"] and "未确认秘密草稿" not in before["body"]
    draft = prepare(pa, "weekly")
    edited = pa.call("report.action", action(draft, action="edit", edits={"0": "完整且确认的周报正文"}))
    pa.call("weekly.confirm", action(edited))
    source.progress[0].summary = "确认后原始数据发生变化"
    monday = datetime.fromisoformat("2026-11-09T09:00:00+08:00")
    manager = scheduled_payloads(source, monday, pa.reports)["manager"]
    assert manager["period"] == "2026-11-02/2026-11-08"
    assert manager["missing"] == ["小李"]
    assert manager["risks"] == ["小王｜recTask：依赖联调"]
    assert "完整且确认的周报正文" in manager["body"]
    assert "确认后原始数据" not in manager["body"] and "未确认秘密草稿" not in manager["body"]
    assert "群进展" not in manager["body"]


def test_s5_whole_body_edit_does_not_leak_old_risk(pa):
    draft = prepare(pa, "weekly")
    edited = pa.call("report.action", action(draft, action="edit", body="风险已排除，正文已经更正"))
    pa.call("weekly.confirm", action(edited))
    manager = scheduled_payloads(report_input(pa), NOW, pa.reports)["manager"]
    assert manager["risks"] == [] and "依赖联调" not in manager["body"]


def test_s6_cli_fixture_dry_run_no_db_or_remote_mutation(tmp_path, capsys):
    db = tmp_path / "must-not-exist" / "reports.db"
    common = [
        "--config",
        str(CONFIG),
        "--fixture",
        str(FIXTURE),
        "--db",
        str(db),
        "--dry-run",
        "--now",
        NOW.isoformat(),
    ]
    assert main(["progress.submit", *common, "--json", '{"task":"recTask","summary":"test: 离线填报"}']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["fields"]["记录人"] == [{"id": "ou_alice"}]
    assert not db.parent.exists()
    for method in ("daily.confirm", "weekly.confirm"):
        assert main([method, *common, "--json", '{"body":"test: 手填报告"}']) == 0
        assert json.loads(capsys.readouterr().out)["result"]["status"] == "confirmed"
        assert not db.exists()
    assert main(["tasks.list", "--config", str(CONFIG), "--fixture", str(FIXTURE)]) == 2
    assert "fixture" in capsys.readouterr().err


def test_cli_auth_master_time_and_bad_json_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FEISHU_USER_ACCESS_TOKEN", raising=False)
    common = ["--config", str(CONFIG), "--db", str(tmp_path / "reports.db")]
    assert main(["tasks.list", *common]) == 2
    assert "未登录" in capsys.readouterr().err
    assert main(["tasks.list", *common, "--actor", "master"]) == 2
    assert "Master" in capsys.readouterr().err
    assert main(["tasks.list", *common, "--now", NOW.isoformat()]) == 2
    assert "时间" in capsys.readouterr().err
    assert main(["tasks.list", *common, "--json", "{"]) == 2
    assert not (tmp_path / "reports.db").exists()


def test_api_dry_run_and_cli_existing_db_unchanged(pa, tmp_path, capsys):
    draft = prepare(pa)
    preview = pa.call("daily.confirm", action(draft), dry_run=True)
    assert preview["result"]["status"] == "confirmed"
    assert pa.reports.get(draft["id"])["status"] == "pending"
    db = tmp_path / "reports.db"
    before = db.read_bytes()
    assert (
        main(
            [
                "daily.confirm",
                "--config",
                str(CONFIG),
                "--fixture",
                str(FIXTURE),
                "--dry-run",
                "--db",
                str(db),
                "--now",
                NOW.isoformat(),
                "--json",
                json.dumps(action(draft)),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result"]["status"] == "confirmed"
    assert db.read_bytes() == before


def test_legacy_database_migration_and_callback_edit_confirm(pa, tmp_path):
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE report_drafts (id TEXT PRIMARY KEY,owner TEXT,kind TEXT,day TEXT,body TEXT,revision TEXT,status TEXT,redrafted INTEGER)"
    )
    db.execute("INSERT INTO report_drafts VALUES ('old','ou_alice','daily','2026-11-06','旧摘要','v0','pending',0)")
    db.commit()
    db.close()
    store = ReportStore(path)
    try:
        event = {
            "operator": {"open_id": "ou_alice"},
            "action": {"value": {"kind": "report_confirm", "report_id": "old", "revision": "v0", "action": "edit"}},
        }
        assert handle_report_action(store, event, NOW.date())["card"]["data"]["elements"][1]["tag"] == "form"
        event["action"]["form_value"] = {"summary": "新摘要"}
        response = handle_report_action(store, event, NOW.date())
        assert response["toast"]["type"] == "success"
        edited = store.get("old")
        assert edited["body"] == "新摘要"
        event["action"] = {"value": {"kind": "report_confirm", **action(edited), "action": "confirm"}}
        assert handle_report_action(store, event, NOW.date())["toast"]["type"] == "success"
        assert handle_report_action(store, event, NOW.date())["toast"]["type"] == "error"
    finally:
        store.close()


def test_atomic_revision_race_and_no_aba(pa):
    draft = prepare(pa)

    def edit(i):
        try:
            return pa.call("report.action", action(draft, action="edit", body=f"修订 {i}"))
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [r for r in pool.map(edit, range(4)) if r]
    assert len(results) == 1
    restored = pa.call("report.action", action(results[0], action="edit", body=draft["body"]))
    assert restored["revision"] != draft["revision"]
    with pytest.raises(ValueError):
        pa.call("daily.confirm", action(draft))


def test_weekly_expires_sunday_and_future_callback_refused(pa):
    draft = prepare(pa, "weekly")
    assert not pa.reports.confirm(draft["id"], draft["revision"], "ou_alice", date(2026, 11, 5))
    assert not pa.reports.confirm(draft["id"], draft["revision"], "ou_alice", date(2026, 11, 9))
    assert pa.reports.confirm(draft["id"], draft["revision"], "ou_alice", date(2026, 11, 8))


def test_confirm_must_not_silently_ignore_edits(pa):
    draft = prepare(pa)
    with pytest.raises(ValueError, match="夹带"):
        pa.call("report.action", action(draft, action="confirm", body="未经展示的新内容"))
    assert pa.reports.get(draft["id"])["status"] == "pending"


def test_progress_remote_delete_and_retry_survive_local_index(pa):
    from bitable_bot.requirements_v09.writer import MultiTableWriter
    from bitable_bot.session import Store
    from bitable_bot.writer import BitableWriter, FeishuAPIError

    store = Store()
    try:
        writer = MultiTableWriter(BitableWriter(pa.api, store, sleep=lambda _: None), pa.cfg.tables)
        values = {"sync_key": "stable", "summary": "第一次"}
        first = writer.upsert_progress(values)
        pa.api.data["progress"] = [r for r in pa.api.data["progress"] if r["record_id"] != first.record_id]
        original = pa.api.create_record
        attempts = []

        def flaky(*args):
            attempts.append(args[-1])
            if len(attempts) == 1:
                raise FeishuAPIError(1254290, "rate limit")
            return original(*args)

        pa.api.create_record = flaky
        recreated = writer.upsert_progress({**values, "summary": "第二次"})
        assert recreated.record_id != first.record_id and pa.api.created == 2
        assert len(set(attempts)) == 1
    finally:
        store.close()


def test_gateway_preserves_edit_card_update(pa):
    from bitable_bot.gateway.ws import Gateway

    class Service:
        def handle_card_action(self, payload):
            return handle_report_action(pa.reports, payload, NOW.date())

    draft = prepare(pa)
    gateway = Gateway(Service())
    try:
        response = gateway.on_card_action(
            {
                "event": {
                    "operator": {"open_id": "ou_alice"},
                    "action": {"value": {"kind": "report_confirm", **action(draft), "action": "edit"}},
                }
            }
        )
        # SDK response, not a test double: card edits must survive the gateway serialization.
        assert response.toast.type == "info"
        assert response.card.type == "raw"
        assert response.card.data["elements"][1]["tag"] == "form"
    finally:
        gateway.pool.shutdown()


def test_manual_pending_report_survives_inbox_and_scheduler_refresh(pa):
    manual = pa.call("report.prepare", {"kind": "daily", "body": "无编码，完成访谈"})
    inbox = pa.call("inbox")
    daily = next(r for r in inbox["confirmations"] if r["kind"] == "daily")
    assert daily["revision"] == manual["revision"]
    assert "无编码，完成访谈" in daily["card"]["elements"][0]["text"]["content"]
    assert pa.call("daily.confirm", action(manual))["status"] == "confirmed"


def test_two_report_connections_only_one_confirmation_wins(pa, tmp_path):
    other = ReportStore(tmp_path / "reports.db")
    draft = prepare(pa)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(
                    lambda store: store.confirm(draft["id"], draft["revision"], "ou_alice", NOW.date()),
                    [pa.reports, other],
                )
            )
        assert sorted(outcomes) == [False, True]
    finally:
        other.close()


def test_unflagged_group_and_flagged_personal_rows_excluded(pa):
    pa.api.data["progress"][1]["fields"]["复核标记"] = False
    pa.api.data["progress"][0]["fields"]["复核标记"] = True
    draft = prepare(pa)
    assert draft["items"] == [] and "无编码/请手填" in draft["body"]
    assert "群进展" not in draft["body"] and "完成主动入口" not in draft["body"]
