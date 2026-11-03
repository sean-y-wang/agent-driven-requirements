import json
from datetime import date, datetime
from pathlib import Path

import pytest

from bitable_bot.requirements_v09.reports import (
    ReportInput,
    ReportStore,
    due_kinds,
    main,
    manager_report,
    scheduled_payloads,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/v09"


def data():
    return ReportInput.model_validate_json((FIXTURES / "reports.json").read_text())


@pytest.mark.parametrize(
    "now,expected",
    [
        ("2026-11-02T08:59:00+08:00", []),
        ("2026-11-02T09:00:00+08:00", ["daily"]),
        ("2026-11-06T17:59:00+08:00", ["daily"]),
        ("2026-11-06T18:00:00+08:00", ["daily", "weekly"]),
        ("2026-11-07T18:00:00+08:00", []),
        ("2026-11-06T10:00:00+00:00", ["daily", "weekly"]),
    ],
)
def test_schedule_timezone_boundaries(now, expected):
    assert due_kinds(datetime.fromisoformat(now)) == expected


def test_payload_snapshot_and_manager_full_body():
    output = scheduled_payloads(data(), datetime.fromisoformat("2026-11-06T18:00:00+08:00"), ReportStore())
    # Revisions are random to prevent an old A→B→A card from becoming valid again.
    def normalize(value):
        if isinstance(value, dict):
            return {k: "<revision>" if k == "revision" else normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value

    assert normalize(output) == json.loads((FIXTURES.parent / "v10/report_cards.json").read_text())
    weekly = [x for x in output["confirmations"] if x["kind"] == "weekly"]
    assert all(not x["allow_interrupt"] for x in weekly)
    body = output["manager"]["body"]
    assert "完成接口联调" not in body and "推进评测接入" not in body and "测试环境阻塞" not in body
    assert output["manager"]["missing"] == ["小王", "小李"]


def test_unconfirmed_redraft_auth_revision_expiry_and_restart(tmp_path):
    db = tmp_path / "reports.db"
    store = ReportStore(db)
    draft = store.draft("ou_alice", "daily", "2026-11-05", "旧内容")
    newer = store.draft("ou_alice", "daily", "2026-11-05", "更正内容")
    assert not store.confirm(draft["id"], draft["revision"], "ou_alice", date(2026, 11, 5))
    assert not store.confirm(newer["id"], newer["revision"], "ou_bob", date(2026, 11, 5))
    assert not store.confirm(newer["id"], newer["revision"], "", date(2026, 11, 5))
    # Daily remains valid through the next calendar day; expires on day+2.
    assert store.valid(newer, newer["revision"], "ou_alice", date(2026, 11, 6))
    assert not store.confirm(newer["id"], newer["revision"], "ou_alice", date(2026, 11, 7))
    store.close()
    store = ReportStore(db)
    next_day = store.draft("ou_alice", "daily", "2026-11-06", "次日重拟")
    assert next_day["redrafted"]
    assert store.confirm(next_day["id"], next_day["revision"], "ou_alice", date(2026, 11, 6))
    assert not store.confirm(next_day["id"], next_day["revision"], "ou_alice", date(2026, 11, 6))
    # Confirmed text is immutable for this scheduled period.
    assert store.draft("ou_alice", "daily", "2026-11-06", "覆盖尝试")["body"] == "次日重拟"


def test_no_truncation_and_missing_report_definition():
    source = data()
    source.progress *= 25
    result = manager_report(source.members, source.progress, date(2026, 11, 6), {"ou_alice"})
    assert result["body"].count("完成接口联调") == 25
    assert result["missing"] == ["小李"]


def test_scheduler_dry_run_no_database(tmp_path, capsys):
    db = tmp_path / "absent.db"
    assert (
        main(
            [
                "--input",
                str(FIXTURES / "reports.json"),
                "--now",
                "2026-11-06T18:00:00+08:00",
                "--db",
                str(db),
                "--dry-run",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["manager"]["body"]
    assert not db.exists()


def test_live_input_reads_all_rows_and_preserves_owner(monkeypatch, tmp_path, capsys):
    from bitable_bot.requirements_v09.user_api import UserAPI
    from tests.unit.test_v09_tables import CONFIG

    members = tmp_path / "members.json"
    members.write_text('[{"open_id":"ou_alice","name":"小王"}]')
    monkeypatch.setenv("FEISHU_USER_ACCESS_TOKEN", "fixture")
    monkeypatch.setattr(UserAPI, "identity", lambda self: "ou_manager")
    monkeypatch.setattr(
        UserAPI,
        "list_all_records",
        lambda *args: [
            {
                "fields": {
                    "记录人": [{"id": "ou_alice"}],
                    "日期": [{"text": "2026-11-05"}],
                    "进展摘要": [{"text": "完成现场联调"}],
                    "任务记录标识": "recOne",
                    "所属模块": "AICloud Labs",
                    "来源智能体": "Codex",
                    "复核标记": False,
                }
            }
        ],
    )
    assert (
        main(["--config", str(CONFIG), "--members", str(members), "--dry-run", "--now", "2026-11-06T18:00:00+08:00"])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert "完成现场联调" not in result["manager"]["body"]
    assert "完成现场联调" in result["confirmations"][0]["card"]["elements"][0]["text"]["content"]
    assert result["confirmations"][0]["recipient"] == "ou_alice"
