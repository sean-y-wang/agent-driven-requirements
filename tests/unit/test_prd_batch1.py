"""PRD 第一批：首次提需问部门、附件转存、我的任务、延期通知、成员配置表。"""

import json
from datetime import date

import pytest

from bitable_bot.config import load_config
from bitable_bot.extractor import Extractor
from bitable_bot.requirements_v09.prd import (
    MemberDirectory,
    extract_attachments,
    overdue_notices,
    parse_dept,
    parse_task_command,
    run_overdue,
)
from bitable_bot.router import IncomingMessage
from bitable_bot.service import BotService
from bitable_bot.session import Store
from bitable_bot.writer import BitableWriter
from tests.conftest import ROOT, FakeAPI, FakeProvider, make_msg

CONFIG = ROOT / "config" / "roles.requirements.xfchat.example.yaml"
DAY_MS = 86400 * 1000


class TableAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.tables: dict[str, list[dict]] = {}
        self.updates: list[tuple] = []
        self.uploads: list[tuple] = []

    def create_record(self, app_token, table_id, fields, client_token):
        rid = super().create_record(app_token, table_id, fields, client_token)
        self.tables.setdefault(table_id, []).append({"record_id": rid, "fields": dict(fields)})
        return rid

    def list_all_records(self, app_token, table_id):
        return [dict(r) for r in self.tables.get(table_id, [])]

    def update_record(self, app_token, table_id, record_id, fields):
        self.updates.append((table_id, record_id, fields))
        for r in self.tables.get(table_id, []):
            if r["record_id"] == record_id:
                r["fields"].update(fields)

    def download_resource(self, message_id, file_key, kind):
        return b"x" * 8, f"{file_key}.bin"

    def upload_media(self, app_token, file_name, data, parent_type):
        self.uploads.append((file_name, parent_type))
        return f"tok_{len(self.uploads)}"


@pytest.fixture
def env():
    cfg = load_config(CONFIG, env={})
    cfg.first_guide_enabled = False
    cfg.ack_after_seconds = 0
    cfg.react_on_mention = False
    cfg.roles[0].confirm_before_write = False
    cfg.overdue_notify.dry_run = True  # 单测先走 dry_run，再显式打开实发
    cfg.ops_review.enabled = False  # 避免入池副作用打扰断言
    api = TableAPI()
    api.cards_ok = True
    store = Store()
    llm = FakeProvider()
    svc = BotService(cfg, api, store, Extractor({k: llm for k in cfg.llm.providers}, cfg.llm.default),
                     BitableWriter(api, store), bot_open_id="ou_bot")
    return svc, api, store, llm, cfg


def _intent():
    return {"action": "record", "role": "/提需求", "confidence": 0.95, "reason": "t"}


def _extract(title="[冒烟] 支持数据批量导出"):
    return {"fields": {"title": title, "desc": "分析同事需要批量导出数据"}, "missing": [], "confidence": {}}


def _p2p(text, sender="ou_req", **kw):
    return make_msg(text, chat_type="p2p", at_bot=False, sender=sender, chat=f"oc_p2p_{sender}", **kw)


def _confirm(svc, api, sender):
    card = next(r for r in reversed(api.replies) if r.get("type") == "interactive")["card"]
    value = card["elements"][-1]["actions"][0]["value"]
    return svc.handle_card_action({"operator": {"open_id": sender}, "action": {"value": value}})


def _req_rows(api, cfg):
    return api.tables.get(cfg.tables.requirements.table_id, [])


def test_first_request_asks_dept_then_reuses(env):
    svc, api, store, llm, cfg = env
    llm.outputs = [_intent(), _extract()]
    svc.handle(_p2p("帮我提个需求：支持数据批量导出给分析同事用"))
    assert "第一次提需" in api.replies[-1]["text"]
    assert not _req_rows(api, cfg)
    svc.handle(_p2p("4"))  # 答部门后 PRD 直接入池，无确认卡
    row = _req_rows(api, cfg)[0]["fields"]
    assert row["提出部门"] == ["输入法"]
    member = api.tables[cfg.tables.members.table_id][0]["fields"]
    assert member["成员"] == [{"id": "ou_req"}] and member["提出部门"] == "输入法" and member["角色"] == "需求方"
    # second request: no question, dept reused, direct write
    llm.outputs = [_intent(), _extract("[冒烟] 第二条需求，导出支持筛选")]
    n = len(api.replies)
    svc.handle(_p2p("再提一个：导出要支持按日期筛选"))
    assert all("第一次提需" not in (r.get("text") or "") for r in api.replies[n:])
    assert _req_rows(api, cfg)[1]["fields"]["提出部门"] == ["输入法"]


def test_dept_skip_is_remembered(env):
    svc, api, store, llm, cfg = env
    llm.outputs = [_intent(), _extract()]
    svc.handle(_p2p("帮我提个需求：支持数据批量导出给分析同事用", sender="ou_s"))
    svc.handle(_p2p("跳过", sender="ou_s"))
    assert "提出部门" not in _req_rows(api, cfg)[0]["fields"]
    assert store.get_member_dept("ou_s") == []


def test_parse_dept(env):
    spec = next(f for f in env[4].roles[0].fields if f.key == "dept")
    assert parse_dept("1、3", spec) == ["消费者-生态", "汽车座舱"]
    assert parse_dept("座舱", spec) == ["汽车座舱"]
    assert parse_dept("火星", spec) is None


def test_attachment_transferred(env):
    """无确认卡时：先提需进入问部门，会话未关时可收附件，答部门后一并写入。"""
    svc, api, store, llm, cfg = env
    llm.outputs = [_intent(), _extract()]
    svc.handle(_p2p("帮我提个需求：支持数据批量导出给分析同事用", sender="ou_a"))
    assert "第一次提需" in api.replies[-1]["text"]
    img = IncomingMessage(event_id="ev_img", message_id="om_img", chat_id="oc_p2p_ou_a", chat_type="p2p",
                          sender_open_id="ou_a", message_type="image", content=json.dumps({"image_key": "img_k1"}))
    svc.handle(img)
    assert "已收到附件" in api.replies[-1]["text"]
    svc.handle(_p2p("5", sender="ou_a"))  # 研究院
    assert _req_rows(api, cfg)[0]["fields"]["附件"] == [{"file_token": "tok_1"}]
    assert api.uploads == [("img_k1.bin", "bitable_image")]


def test_extract_post_images():
    post = {"title": "", "content": [[{"tag": "text", "text": "看图"}, {"tag": "img", "image_key": "k2"}]]}
    assert extract_attachments("post", json.dumps(post), "om_1")[0]["key"] == "k2"


def test_my_tasks_list_and_status(env):
    svc, api, store, llm, cfg = env
    t = cfg.tables.tasks.table_id
    api.tables[t] = [
        {"record_id": "rec_a", "fields": {"任务标题": "[冒烟] 导出接口", "状态": "待开发", "开发": [{"id": "ou_dev"}]}},
        {"record_id": "rec_b", "fields": {"任务标题": "[冒烟] 别人的任务", "状态": "待开发", "开发": [{"id": "ou_x"}]}},
    ]
    svc.handle(_p2p("我的任务", sender="ou_dev"))
    assert "导出接口" in api.replies[-1]["text"] and "别人的任务" not in api.replies[-1]["text"]
    svc.handle(_p2p("1 改为 开发中", sender="ou_dev"))
    assert api.tables[t][0]["fields"]["状态"] == "开发中"
    svc.handle(_p2p("把 别人的任务 改成 已上线", sender="ou_dev"))
    assert api.tables[t][1]["fields"]["状态"] == "待开发"
    assert "只能改分给自己的任务" in api.replies[-1]["text"]


def test_parse_task_command():
    st = ["开发中", "已上线"]
    assert parse_task_command("看看我的任务", st).kind == "list"
    assert parse_task_command("2 改成 已上线", st).target == "2"
    assert parse_task_command("把按钮改成蓝色", st) is None


def test_overdue_notice_and_dedup(env):
    svc, api, store, llm, cfg = env
    r = cfg.tables.requirements.table_id
    today = date(2026, 11, 3)
    past = int(__import__("datetime").datetime(2026, 10, 30, 12).timestamp() * 1000)
    api.tables[r] = [
        {"record_id": "rec1", "fields": {"需求标题": "[冒烟] 逾期", "状态": "开发中", "计划上线日期": past,
                                          "提出人账号": [{"id": "ou_p"}]}},
        {"record_id": "rec2", "fields": {"需求标题": "[冒烟] 已上线", "状态": "已上线", "计划上线日期": past,
                                          "提出人账号": [{"id": "ou_p"}]}},
        {"record_id": "rec3", "fields": {"需求标题": "[冒烟] 有受理人", "状态": "待开发", "计划上线日期": past,
                                          "需求受理人": [{"id": "ou_owner"}], "提出人账号": [{"id": "ou_p"}]}},
        {"record_id": "rec4", "fields": {"需求标题": "[冒烟] 未到期", "状态": "待开发",
                                          "计划上线日期": past + 10 * DAY_MS, "提出人账号": [{"id": "ou_p"}]}},
    ]
    notes = overdue_notices(api, cfg.tables, today, cfg.timezone, cfg.overdue_notify.done_statuses)
    assert [(n["record_id"], n["to"]) for n in notes] == [("rec1", ["ou_p"]), ("rec3", ["ou_owner"])]
    cfg.overdue_notify.enabled = True
    assert run_overdue(svc, today) == 0  # dry_run default
    cfg.overdue_notify.dry_run = False
    assert run_overdue(svc, today) == 2
    assert run_overdue(svc, today) == 0  # once per record + plan date


def test_member_directory_fallback(env, tmp_path):
    svc, api, store, llm, cfg = env
    j = tmp_path / "m.json"
    j.write_text(json.dumps([{"open_id": "ou_j", "name": "J"}]))
    d = MemberDirectory(api, cfg.tables, fallback_json=j)
    assert d.report_members() == [{"open_id": "ou_j", "name": "J"}]
    api.tables[cfg.tables.members.table_id] = [
        {"record_id": "m1", "fields": {"姓名": "A", "成员": [{"id": "ou_a"}], "角色": "参与者"}},
        {"record_id": "m2", "fields": {"姓名": "R", "成员": [{"id": "ou_r"}], "角色": "需求方", "提出部门": "输入法"}},
    ]
    d = MemberDirectory(api, cfg.tables, fallback_json=j)
    assert d.report_members() == [{"open_id": "ou_a", "name": "A"}]
    assert d.dept_of("ou_r") == ["输入法"]
