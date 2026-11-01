import pytest

from bitable_bot.config import load_config
from bitable_bot.extractor import Extractor
from bitable_bot.service import BotService
from bitable_bot.session import Store
from bitable_bot.writer import BitableWriter
from tests.conftest import FakeAPI, FakeProvider, make_msg
from tests.unit.test_v09_tables import CONFIG


@pytest.fixture
def intake():
    cfg = load_config(CONFIG, env={})
    cfg.first_guide_enabled = False
    cfg.ack_after_seconds = 0
    cfg.intake.observe_groups = True
    cfg.intake.all_messages_permission = True
    cfg.intake.observed_chats = ["oc_1"]
    api = FakeAPI()
    api.cards_ok = True
    store = Store()
    provider = FakeProvider()
    service = BotService(
        cfg,
        api,
        store,
        Extractor({k: provider for k in cfg.llm.providers}, cfg.llm.default),
        BitableWriter(api, store),
        bot_open_id="ou_bot",
    )
    return service, api, store, provider


def classify(action):
    return {"action": action, "role": "/提需求", "confidence": 0.95, "reason": "test"}


def extract():
    return {
        "fields": {"title": "支持数据批量导出", "desc": "用户需要数据批量导出", "status": "已完成"},
        "missing": [],
        "confidence": {},
    }


def test_explicit_requirement_confirms_then_review(intake):
    svc, api, store, llm = intake
    llm.outputs = [classify("record"), extract()]
    msg = make_msg("请提一个需求，支持数据批量导出给分析同事")
    svc.handle(msg)
    assert not api.records
    card = api.replies[-1]["card"]
    assert "待评审" in str(card)
    action = card["elements"][-1]["actions"][0]["value"]
    svc.handle_card_action({"operator": {"open_id": msg.sender_open_id}, "action": {"value": action}})
    assert api.records[0]["fields"]["状态"] == "待评审"
    assert not api.records[0]["fields"]["复核标记"]
    assert not store.sessions_for(msg.chat_id, msg.sender_open_id)


@pytest.mark.parametrize("action,column,value", [("candidate", "状态", "待分诊"), ("progress", "来源智能体", "群")])
def test_passive_no_confirmation(intake, action, column, value):
    svc, api, store, llm = intake
    llm.outputs = [classify(action)] + ([extract()] if action == "candidate" else [])
    msg = make_msg("数据导出任务已经完成，等待明天上线", at_bot=False)
    svc.handle(msg)
    assert len(api.records) == 1
    row = api.records[0]
    assert row["fields"][column] == value
    assert row["fields"]["复核标记"] is True
    assert row["table_id"] == ("tblPROGRESS" if action == "progress" else "tblREQUIREMENTS")
    assert not api.replies
    assert not store.sessions_for(msg.chat_id, msg.sender_open_id)
    svc.handle(msg)
    assert len(api.records) == 1


def test_passive_scope_chitchat_failure_and_bot_ignored(intake):
    svc, api, store, llm = intake
    svc.handle(make_msg("任务完成", chat="oc_other", at_bot=False))
    msg = make_msg("任务完成", at_bot=False)
    msg.sender_type = "app"
    svc.handle(msg)
    svc.handle(make_msg("你好", at_bot=False))
    llm.outputs = [{"action": None, "role": None, "confidence": 0, "reason": "unknown"}]
    svc.handle(make_msg("一些不确定的讨论", at_bot=False))
    assert not api.records and not api.replies


def test_report_callback_requires_owner_and_current_revision(intake):
    from datetime import datetime

    from bitable_bot.requirements_v09.reports import ReportStore

    svc, api, _, _ = intake
    svc.report_store = ReportStore()
    svc.clock = lambda: datetime.fromisoformat("2026-11-06T09:01:00+08:00").timestamp()
    draft = svc.report_store.draft("ou_alice", "daily", "2026-11-06", "完成联调")
    value = {"kind": "report_confirm", "report_id": draft["id"], "revision": draft["revision"]}
    assert svc.handle_card_action({"action": {"value": value}})["toast"]["type"] == "error"
    assert (
        svc.handle_card_action({"operator": {"open_id": "ou_bob"}, "action": {"value": value}})["toast"]["type"]
        == "error"
    )
    assert (
        svc.handle_card_action({"operator": {"open_id": "ou_alice"}, "action": {"value": value}})["toast"]["type"]
        == "success"
    )
    assert not api.records


def test_passive_candidate_does_not_replace_open_requirement(intake):
    svc, api, store, llm = intake
    llm.outputs = [classify("record"), extract(), classify("candidate"), extract()]
    msg = make_msg("请提需求，增加批量导出到电子表格功能")
    svc.handle(msg)
    session = store.sessions_for(msg.chat_id, msg.sender_open_id)[0]
    api.replies.clear()
    svc.handle(make_msg("如果可以按周批量导出统计结果就方便了", at_bot=False))
    assert not api.replies
    assert store.sessions_for(msg.chat_id, msg.sender_open_id)[0].source_message_id == session.source_message_id


def test_missing_group_permission_fails_startup(monkeypatch, tmp_path):
    import bitable_bot.main as entrypoint
    from bitable_bot.config import ConfigError
    from bitable_bot.main import Settings, build_service

    cfg = load_config(CONFIG)
    cfg.intake.observe_groups = True
    cfg.intake.all_messages_permission = True
    cfg.intake.observed_chats = ["oc_1"]
    api = FakeAPI()
    api.fail.add("list_messages:chat")
    monkeypatch.setattr(entrypoint, "LarkFeishuAPI", lambda _: api)
    settings = Settings(
        {"FEISHU_APP_ID": "fixture", "FEISHU_APP_SECRET": "fixture", "BOT_DB_PATH": str(tmp_path / "bot.db")}
    )
    with pytest.raises(ConfigError, match="群全部消息可读权限"):
        build_service(settings, cfg, None)


def test_fresh_group_progress_does_not_edit_pending_confirmation(intake):
    svc, api, store, llm = intake
    llm.outputs = [classify("record"), extract(), classify("progress")]
    msg = make_msg("请提一个需求，支持批量下载全部报表")
    svc.handle(msg)
    api.replies.clear()
    svc.handle(make_msg("联调任务目前已经完成，等待验收后上线"))
    assert not api.replies
    assert api.records[0]["table_id"] == "tblPROGRESS"
    assert store.sessions_for(msg.chat_id, msg.sender_open_id)[0].source_message_id == msg.message_id


def test_private_requirement_retains_source_after_card_confirmation(intake):
    svc, api, _, llm = intake
    llm.outputs = [classify("record"), extract()]
    msg = make_msg("请提需求，增加报表的批量导出能力", chat_type="p2p", at_bot=False)
    svc.handle(msg)
    action = api.replies[-1]["card"]["elements"][-1]["actions"][0]["value"]
    svc.handle_card_action({"operator": {"open_id": msg.sender_open_id}, "action": {"value": action}})
    assert api.records[0]["fields"]["来源智能体"] == "其他"
