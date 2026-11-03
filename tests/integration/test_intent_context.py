"""Natural-language intent, conversation context, traceability placeholders, p2p and group filtering."""

import json

from bitable_bot.writer import FeishuAPIError

from ..conftest import NOW, make_msg


def out(**fields):
    return {"fields": fields, "missing": [k for k, v in fields.items() if v is None], "confidence": {}}


def intent(role, conf=0.9):
    return {"role": role, "confidence": conf, "reason": "test"}


def item(mid, sender, text, ts_ms, sender_type="user"):
    return {
        "message_id": mid,
        "msg_type": "text",
        "create_time": str(ts_ms),
        "sender": {"id": sender, "sender_type": sender_type},
        "body": {"content": json.dumps({"text": text}, ensure_ascii=False)},
    }


T0 = int(NOW * 1000)


def test_plain_mention_high_confidence_writes(harness):
    h = harness
    h.api.users = {"ou_alice": "Alice"}
    h.api.chat_names = {"oc_1": "研发群"}
    h.provider.outputs = [
        intent("/bug", 0.92),
        out(title="登录页白屏", severity="P1", owner=None, due=None, proposer=None),
    ]
    m = make_msg("登录页白屏了，P1")
    h.api.messages[m.message_id] = {"message_id": m.message_id, "message_position": "42"}
    h.api.history[("chat", "oc_1")] = [
        item("om_x1", "ou_bob", "生产环境登录页打不开", T0 - 60000),
        item(m.message_id, "ou_alice", "@_user_1 登录页白屏了，P1", T0),
    ]
    h.api.members = [("Bob", "ou_bob")]
    h.service.handle(m)
    f = h.api.records[0]["fields"]
    assert f["标题"] == "登录页白屏" and f["严重程度"] == "P1"
    assert f["提出人"] == "Alice" and f["来源群"] == "研发群"
    assert f["原始消息"] == "登录页白屏了，P1"
    assert "Bob：生产环境登录页打不开" in f["上下文摘录"] and "登录页白屏了" not in f["上下文摘录"]
    assert f["消息链接"]["link"] == "https://applink.feishu.cn/client/chat/open?openChatId=oc_1&position=42"
    assert f["提交时间"] == T0
    # intent call then extraction call; the extraction prompt carries the context; auto fields not asked to LLM
    assert len(h.provider.calls) == 2
    ext = h.provider.calls[1]
    assert "生产环境登录页打不开" in ext[1]["content"]
    assert "raw_text" not in ext[0]["content"] and "message_link" not in ext[0]["content"]
    reply = h.api.replies[-1]["text"]
    assert reply.startswith("✅ 需求已收录") and "上下文摘录" not in reply and "原始消息" not in reply


def test_low_confidence_asks_then_yes(harness):
    h = harness
    h.provider.outputs = [
        intent("/bug", 0.4),
        out(title="支付超时", severity="P2", owner=None, due=None, proposer=None),
    ]
    m = make_msg("支付偶尔超时")
    h.service.handle(m)
    ask = h.api.replies[-1]
    assert ask["in_thread"] and "我猜你想记录到「缺陷登记」" in ask["text"] and not h.api.records
    h.service.handle(make_msg("是", root_id=m.message_id, parent_id=ask["id"], at_bot=False))
    rec = h.api.records[0]["fields"]
    assert rec["标题"] == "支付超时" and rec["原始消息"] == "支付偶尔超时"
    assert h.api.replies[-1]["message_id"] == m.message_id
    assert h.store.sessions_for("oc_1", "ou_alice") == []


def test_unknown_role_pick_by_name(harness):
    h = harness
    h.provider.outputs = [intent(None, 0), out(done="写接口", plan=None, status=None, hours=None, tags=None, link=None)]
    m = make_msg("今天写接口")
    h.service.handle(m)
    assert "我不确定要记录到哪张表" in h.api.replies[-1]["text"]
    h.service.handle(make_msg("研发日报", root_id=m.message_id, at_bot=False))
    assert h.api.records[0]["fields"]["今日完成"] == "写接口"


def test_pick_gives_up_after_max_followups(harness):
    h = harness
    h.provider.outputs = [intent(None, 0)]
    m = make_msg("嗯")
    h.service.handle(m)
    for _ in range(h.service.cfg.session.max_followups):
        h.service.handle(make_msg("不知道", root_id=m.message_id, at_bot=False))
        assert "没识别出你选的角色" in h.api.replies[-1]["text"]
    h.service.handle(make_msg("不知道", root_id=m.message_id, at_bot=False))
    assert "已放弃" in h.api.replies[-1]["text"] and not h.api.records
    assert h.store.sessions_for("oc_1", "ou_alice") == []


def test_intent_error_falls_back_to_pick(harness):
    h = harness
    h.provider.outputs = ["not json"]
    h.service.handle(make_msg("记一下"))
    assert "我不确定要记录到哪张表" in h.api.replies[-1]["text"]


def test_intent_disabled_requires_command(harness):
    h = harness
    h.service.cfg.intent.enabled = False
    h.service.handle(make_msg("记一下"))
    assert ("可用命令" in h.api.replies[-1]["text"] or "/日报" in h.api.replies[-1]["text"]) and not h.provider.calls


def test_slash_command_skips_intent(harness):
    h = harness
    h.provider.outputs = [out(title="x", severity="P2", owner=None, due=None, proposer=None)]
    h.service.handle(make_msg("/bug x"))
    assert len(h.provider.calls) == 1 and h.api.records


def test_group_message_without_mention_is_ignored(harness):
    h = harness
    h.service.handle(make_msg("大家好", at_bot=False))
    assert not h.api.replies and not h.provider.calls


def test_p2p_without_mention_triggers(harness):
    h = harness
    h.provider.outputs = [intent("/bug", 0.95), out(title="x", severity="P2", owner=None, due=None, proposer=None)]
    m = make_msg("x 页面报错", at_bot=False, chat="oc_p2p", chat_type="p2p")
    h.service.handle(m)
    f = h.api.records[0]["fields"]
    assert f["来源群"] == "单聊" and f["原始消息"] == "x 页面报错"
    assert ("get_chat_name",) not in h.api.calls


def test_p2p_disabled(harness):
    h = harness
    h.service.cfg.p2p_enabled = False
    h.service.handle(make_msg("/help", at_bot=False, chat_type="p2p"))
    assert not h.api.replies


def test_bot_open_id_mention_detection(harness):
    h = harness
    h.service.bot_open_id = "ou_bot"
    # bot mentioned at the end (not leading) still counts when the id is known
    m = make_msg("看下", at_bot=False, mentions=[("记录机器人", "ou_bot")])
    m.content = json.dumps({"text": "/help @_user_2"}, ensure_ascii=False)
    h.service.handle(m)
    assert "/bug" in h.api.replies[-1]["text"]
    # a mention of someone else only: ignored
    h.service.handle(make_msg("hi", at_bot=False, mentions=[("张三", "ou_z")]))
    assert len(h.api.replies) == 1


def test_context_api_failures_degrade(harness):
    h = harness
    h.api.fail = {
        "get_message",
        "list_messages:chat",
        "list_messages:thread",
        "get_user_name",
        "get_chat_name",
        "list_chat_members",
    }
    h.provider.outputs = [out(title="t", severity="P2", owner=None, due=None, proposer=None)]
    h.service.handle(make_msg("/bug t", parent_id="om_parent", thread_id="omt_1"))
    f = h.api.records[0]["fields"]
    assert f["提出人"] == "ou_alice" and f["来源群"] == "oc_1"
    assert f["消息链接"]["link"] == "https://applink.feishu.cn/client/chat/open?openChatId=oc_1"
    assert "上下文摘录" not in f


def test_parent_and_thread_context(harness):
    h = harness
    h.api.members = [("Bob", "ou_bob")]
    h.api.messages["om_parent"] = item("om_parent", "ou_bob", "首页加载要 8 秒", T0 - 5000)
    h.api.history[("thread", "omt_1")] = [
        item("om_parent", "ou_bob", "首页加载要 8 秒", T0 - 5000),
        item("om_t2", "cli_bot", "收到", T0 - 4000, sender_type="app"),
    ]
    h.service.cfg.context.recent_messages = 0
    h.provider.outputs = [out(title="首页慢", severity="P2", owner=None, due=None, proposer=None)]
    h.service.handle(make_msg("/bug 记一下", parent_id="om_parent", thread_id="omt_1"))
    ctx = h.api.records[0]["fields"]["上下文摘录"]
    assert "【被回复/引用的消息】" in ctx and "Bob：首页加载要 8 秒" in ctx
    assert "【所在话题】" in ctx and "机器人：收到" in ctx
    assert ctx.count("首页加载要 8 秒") == 1


def test_command_with_empty_body_uses_context(harness):
    h = harness
    h.api.messages["om_parent"] = item("om_parent", "ou_bob", "导出按钮没反应", T0 - 5000)
    h.provider.outputs = [out(title="导出按钮没反应", severity="P2", owner=None, due=None, proposer=None)]
    h.service.handle(make_msg("/bug", parent_id="om_parent"))
    assert h.api.records[0]["fields"]["标题"] == "导出按钮没反应"


def test_feishu_error_on_history_is_not_fatal(harness):
    h = harness
    h.api.fail = {"list_messages:chat"}
    h.provider.outputs = [intent("/bug", 0.9), out(title="t", severity="P2", owner=None, due=None, proposer=None)]
    h.service.handle(make_msg("t 报错"))
    assert h.api.records
    assert isinstance(FeishuAPIError(1, "x"), Exception)
