"""End-to-end pipeline with fake Feishu API + fake LLM: message → follow-up → write → receipt."""

from bitable_bot.providers.base import LLMError
from bitable_bot.writer import FeishuAPIError

from ..conftest import NOW, make_msg

DAY = 1792684800000


LINK = "https://applink.feishu.cn/client/chat/open?openChatId=oc_1"


def out(**fields):
    return {"fields": fields, "missing": [k for k, v in fields.items() if v is None], "confidence": {}}


def test_complete_message_written(harness):
    h = harness
    h.provider.outputs = [out(done="完成登录模块", plan="联调", status="顺利", hours="八", tags=["server"])]
    h.service.handle(make_msg("/日报 今天完成了登录模块，明天联调，8小时，后端"))
    rec = h.api.records[0]
    assert rec["app_token"] == "bascnXXXX" and rec["table_id"] == "tblXXXX"
    assert rec["fields"] == {
        "提交人": [{"id": "ou_alice"}],
        "日期": DAY,
        "今日完成": "完成登录模块",
        "明日计划": "联调",
        "工时": 8,
        "状态": "正常",
        "标签": ["后端"],
        # traceability fields filled by the system
        "原始消息": "/日报 今天完成了登录模块，明天联调，8小时，后端",
        "来源群": "oc_1",  # chat name unknown -> chat_id
        "消息链接": {"link": LINK, "text": LINK},
        "提交时间": int(NOW * 1000),
    }
    assert "原始消息" not in h.api.replies[-1]["text"]  # receipt hides raw text
    reply = h.api.replies[-1]["text"]
    assert reply.startswith("✅ 需求已收录") and "日期：2026-10-23" in reply and "record=rec1" in reply
    assert h.store.audit_rows()[-1]["result"] == "ok"
    # the user's text is fenced in the prompt
    assert "<<<USER_TEXT>>>\n今天完成了登录模块" in h.provider.calls[0][1]["content"]


def test_followup_in_thread_then_write(harness):
    h = harness
    h.provider.outputs = [out(title=None, severity="紧急", owner=["张三"]), out(title="登录页白屏")]
    m1 = make_msg("/bug 线上紧急，负责人 @张三", mentions=[("张三", "ou_z")])
    h.service.handle(m1)
    ask = h.api.replies[-1]
    assert ask["in_thread"] and "标题" in ask["text"] and not h.api.records
    assert h.store.audit_rows()[-1]["result"] == "followup"
    # reply in the bot's thread without @/command
    h.service.handle(make_msg("登录页白屏", root_id=m1.message_id, parent_id=ask["id"]))
    rec = h.api.records[0]["fields"]
    assert rec == {
        "标题": "登录页白屏",
        "严重程度": "P0",
        "负责人": [{"id": "ou_z"}],
        "提出人": "ou_alice",  # no contact/member name available -> open_id
        "原始消息": "/bug 线上紧急，负责人 @张三",
        "来源群": "oc_1",
        "消息链接": {"link": LINK, "text": LINK},
        "提交时间": int(NOW * 1000),
    }
    assert "补充回答" in h.provider.calls[1][1]["content"]
    assert h.api.replies[-1]["message_id"] == m1.message_id
    assert "负责人：张三" in h.api.replies[-1]["text"]
    assert h.store.sessions_for("oc_1", "ou_alice") == []


def test_followup_by_mention_again(harness):
    h = harness
    h.provider.outputs = [out(done=None), out(done="写文档")]
    h.service.handle(make_msg("/日报 今天"))
    h.service.handle(make_msg("写了文档"))  # @bot again, no command
    assert h.api.records[0]["fields"]["今日完成"] == "写文档"


def test_max_followups_abandons(harness):
    h = harness
    h.provider.outputs = [out(title=None)] * 3
    h.service.handle(make_msg("/bug 有问题"))
    h.service.handle(make_msg("嗯"))
    h.service.handle(make_msg("不知道"))
    assert not h.api.records
    assert "这次先不算了" in h.api.replies[-1]["text"]
    assert h.store.sessions_for("oc_1", "ou_alice") == []


def test_session_timeout_sweeper(harness):
    h = harness
    h.provider.outputs = [out(title=None)]
    m = make_msg("/bug 有问题")
    h.service.handle(m)
    assert h.service.expire_sessions() == 0
    h.clock.t += 31 * 60
    assert h.service.expire_sessions() == 1
    assert "超时" in h.api.replies[-1]["text"] and h.api.replies[-1]["message_id"] == m.message_id
    h.provider.outputs = [{"role": None, "confidence": 0, "reason": "chat"}]
    h.service.handle(make_msg("补充"))  # no session any more -> intent detection -> ask to pick a role
    assert "我不确定要记录到哪张表" in h.api.replies[-1]["text"]


def test_cancel(harness):
    h = harness
    h.provider.outputs = [out(title=None)]
    h.service.handle(make_msg("/bug x"))
    h.service.handle(make_msg("/取消"))
    assert h.api.replies[-1]["text"] == "已取消未完成的记录。"
    h.service.handle(make_msg("/取消"))
    assert h.api.replies[-1]["text"] == "当前没有未完成的记录。"


def test_dedup_event_and_message(harness):
    h = harness
    h.provider.outputs = [out(title="t")]
    m = make_msg("/bug t", message_id="om_dup", event_id="ev_dup")
    h.service.handle(m)
    h.service.handle(m)  # same event redelivered
    m2 = make_msg("/bug t", message_id="om_dup", event_id="ev_other")  # new event_id, same message
    h.service.handle(m2)
    assert len(h.api.records) == 1 and len(h.provider.calls) == 1


def test_help_unknown_plain_and_nontext(harness):
    h = harness
    h.service.handle(make_msg("/help"))
    assert "/日报" in h.api.replies[-1]["text"]
    h.service.handle(make_msg("/xyz hi"))
    assert "没认出命令 /xyz" in h.api.replies[-1]["text"]
    h.provider.outputs = [{"role": None, "confidence": 0.1, "reason": "greeting"}]
    h.service.handle(make_msg("你好"))
    # greetings / chitchat → help guide (not role-pick / record)
    assert "/日报" in h.api.replies[-1]["text"] or "可用命令" in h.api.replies[-1]["text"]
    h.service.handle(make_msg("/取消"))
    h.service.handle(make_msg("/bug"))
    assert "请在命令后附上内容" in h.api.replies[-1]["text"]
    m = make_msg("x")
    m.message_type = "image"
    h.service.handle(m)
    assert "文字消息" in h.api.replies[-1]["text"]


def test_allowlists(harness, cfg):
    h = harness
    cfg.chats_allowlist = ["oc_ok"]
    h.service.handle(make_msg("/bug x", chat="oc_no"))
    assert "未开通" in h.api.replies[-1]["text"]
    cfg.roles[1].allowed_users = ["ou_admin"]
    h.service.handle(make_msg("/bug x", chat="oc_ok"))
    assert "没有使用「缺陷登记」的权限" in h.api.replies[-1]["text"]
    assert not h.provider.calls


def test_llm_failure_keeps_session_for_retry(harness):
    h = harness
    h.provider.outputs = [out(title=None), LLMError("down"), out(title="ok")]
    h.service.handle(make_msg("/bug x"))
    h.service.handle(make_msg("标题是 ok"))
    assert "刚才没整理好" in h.api.replies[-1]["text"]
    assert h.store.audit_rows()[-1]["result"].startswith("llm_error")
    h.service.handle(make_msg("标题是 ok"))
    assert h.api.records[0]["fields"]["标题"] == "ok"


def test_write_permission_error(harness):
    h = harness
    h.provider.outputs = [out(title="t")]
    h.api.create_errors = [FeishuAPIError(91403, "Forbidden")]
    h.service.handle(make_msg("/bug t"))
    assert "协作者" in h.api.replies[-1]["text"]
    assert h.store.audit_rows()[-1]["result"] == "write_error:91403"


def test_already_written_message(harness):
    h = harness
    h.store.put_record("om_x", "rec9", "https://u")
    h.service.handle(make_msg("/bug t", message_id="om_x"))
    assert "这条已经记过了" in h.api.replies[-1]["text"] and not h.provider.calls


def test_internal_error_replies(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(h.service, "_handle", lambda m: 1 / 0)
    h.service.handle(make_msg("/bug t"))
    assert "内部错误" in h.api.replies[-1]["text"]


def test_user_name_resolved_from_chat_members(harness):
    h = harness
    h.api.members = [("李四", "ou_l")]
    h.provider.outputs = [out(title="t", owner=["李四"])]
    h.service.handle(make_msg("/bug t 负责人李四"))
    assert h.api.records[0]["fields"]["负责人"] == [{"id": "ou_l"}]


def test_reply_failure_does_not_crash(harness):
    h = harness

    def boom(*a, **k):
        raise FeishuAPIError(230002, "bot not in chat")

    h.api.reply_text = boom
    h.service.handle(make_msg("/help"))  # should not raise


def test_new_command_replaces_open_session(harness):
    h = harness
    h.provider.outputs = [out(title=None), out(title="second")]
    h.service.handle(make_msg("/bug first"))
    h.service.handle(make_msg("/bug second"))
    assert h.api.records[0]["fields"]["标题"] == "second"
    assert h.store.sessions_for("oc_1", "ou_alice") == []
