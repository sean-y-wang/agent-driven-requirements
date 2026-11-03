"""Usability: confirm / guide / first tip / duplicate link / card; progress query."""

import json

from ..conftest import make_msg


def out(**fields):
    return {"fields": fields, "missing": [k for k, v in fields.items() if v is None], "confidence": {}}


def intent(role, conf=0.9, action="record", query="", keywords=None):
    return {
        "action": action,
        "role": role,
        "confidence": conf,
        "query": query or None,
        "keywords": keywords,
        "reason": "t",
    }


def test_thin_content_guides_without_extract(harness):
    h = harness
    role = h.service.cfg.find_role("/bug")
    role.min_content_chars = 12
    role.guide_prompt = "请说清标题和严重程度"
    h.service.handle(make_msg("/bug 嗯"))
    assert "请说清标题和严重程度" in h.api.replies[-1]["text"]
    assert not h.provider.calls and not h.api.records


def test_confirm_before_write_yes(harness):
    h = harness
    h.service.cfg.find_role("/bug").confirm_before_write = True
    h.provider.outputs = [out(title="白屏", severity="P1", owner=None, due=None, proposer=None)]
    m = make_msg("/bug 登录页白屏 P1")
    h.service.handle(m)
    assert not h.api.records
    ask = h.api.replies[-1]
    assert ask["in_thread"] and "请确认" in ask["text"] and "白屏" in ask["text"]
    h.service.handle(make_msg("确认", root_id=m.message_id, parent_id=ask["id"], at_bot=False))
    assert h.api.records and h.api.records[0]["fields"]["标题"] == "白屏"
    assert "✅" in h.api.replies[-1]["text"] or "已记录" in h.api.replies[-1]["text"] or "已收录" in h.api.replies[-1]["text"]


def test_confirm_edit_severity(harness):
    h = harness
    h.service.cfg.find_role("/bug").confirm_before_write = True
    h.provider.outputs = [out(title="白屏", severity="P2", owner=None, due=None, proposer=None)]
    m = make_msg("/bug 白屏")
    h.service.handle(m)
    ask = h.api.replies[-1]
    h.service.handle(make_msg("严重程度改成 P0", root_id=m.message_id, parent_id=ask["id"], at_bot=False))
    preview = h.api.replies[-1]["text"]
    assert "P0" in preview and "请确认" in preview and not h.api.records
    h.service.handle(make_msg("好", root_id=m.message_id, at_bot=False))
    assert h.api.records[0]["fields"]["严重程度"] == "P0"


def test_followup_lists_all_required_allows_skip_copy(harness):
    h = harness
    h.provider.outputs = [out(title=None, severity=None, owner=None, due=None, proposer=None)]
    h.service.handle(make_msg("/bug 出了点问题"))
    text = h.api.replies[-1]["text"]
    assert "标题" in text  # 严重程度 has default P2 → not missing
    assert "选填" in text and "跳过" in text


def test_duplicate_receipt_includes_link(harness):
    h = harness
    h.provider.outputs = [out(title="x", severity="P2", owner=None, due=None, proposer=None)]
    m = make_msg("/bug x", message_id="om_dup")
    h.service.handle(m)
    assert h.api.records
    url = h.store.get_record("om_dup")[1]
    h.service.handle(make_msg("/bug x again", message_id="om_dup", event_id="evt_other"))
    # dedup by message_id may skip entirely — use same source via get_record path:
    # is_duplicate sees msg:om_dup already → skip with no reply. Clear dedup by new message
    # that maps to same logical record via writer — call _start path with same source by
    # simulating store hit:
    h2_msg = make_msg("/bug y", message_id="om_new")
    h.store.put_record(h2_msg.message_id, "rec9", url)
    h.service.handle(h2_msg)
    assert ("已记录过" in h.api.replies[-1]["text"] or "已经记过" in h.api.replies[-1]["text"]) and url in h.api.replies[-1]["text"]


def test_card_receipt_when_enabled(harness):
    h = harness
    h.api.cards_ok = True
    h.provider.outputs = [out(title="x", severity="P2", owner=None, due=None, proposer=None)]
    h.service.handle(make_msg("/bug x"))
    last = h.api.replies[-1]
    assert last["type"] == "interactive" and "打开记录" in json.dumps(last["card"], ensure_ascii=False)


def test_first_guide_once_per_chat(harness):
    h = harness
    h.service.cfg.first_guide_enabled = True
    h.provider.outputs = [
        out(title="a", severity="P2", owner=None, due=None, proposer=None),
        out(title="b", severity="P2", owner=None, due=None, proposer=None),
    ]
    h.service.handle(make_msg("/bug a", chat="oc_new"))
    texts = [r["text"] for r in h.api.replies]
    assert any("直接说需求即可" in t for t in texts)
    n = len(h.api.replies)
    h.service.handle(make_msg("/bug b", chat="oc_new"))
    assert not any("直接说需求即可" in r["text"] for r in h.api.replies[n:])


def test_query_by_intent(harness):
    h = harness
    h.api.bitable_rows = [
        {
            "record_id": "recA",
            "app_token": "bascnYYYY",
            "table_id": "tblYYYY",
            "fields": {"标题": "Acme API 流式", "严重程度": "P1"},
        },
        {
            "record_id": "recB",
            "app_token": "bascnYYYY",
            "table_id": "tblYYYY",
            "fields": {"标题": "登录页白屏", "严重程度": "P0"},
        },
    ]
    h.provider.outputs = [
        intent("/bug", 0.92, action="query", query="Acme API", keywords=["Acme", "API"]),
        {"summary": "找到相关缺陷：", "items": [{"record_id": "recA", "one_liner": "流式返回，P1"}]},
    ]
    h.service.handle(make_msg("Acme API 上线了吗"))
    assert not h.api.records
    reply = h.api.replies[-1]["text"]
    assert "流式" in reply or "Acme" in reply or "找到" in reply
    assert "recA" in reply or "bascnYYYY" in reply
    assert h.store.audit_rows()[-1]["command"] == "query:/bug"
    assert h.store.sessions_for("oc_1", "ou_alice") == []


def test_query_empty_fallback(harness):
    h = harness
    h.api.bitable_rows = [
        {
            "record_id": "recZ",
            "app_token": "bascnYYYY",
            "table_id": "tblYYYY",
            "fields": {"标题": "完全无关的事", "严重程度": "P3"},
        },
    ]
    h.provider.outputs = [
        intent("/bug", 0.9, action="query", query="不存在的XYZ", keywords=["不存在的XYZ"]),
        {"summary": "没有相关记录", "items": []},
    ]
    h.service.handle(make_msg("不存在的XYZ 进展怎样"))
    assert "没找到" in h.api.replies[-1]["text"] or "没有" in h.api.replies[-1]["text"]


def test_slash_query_hint(harness):
    h = harness
    h.api.bitable_rows = [
        {
            "record_id": "recA",
            "app_token": "bascnYYYY",
            "table_id": "tblYYYY",
            "fields": {"标题": "支付超时", "严重程度": "P1"},
        },
    ]
    h.provider.outputs = [
        {"summary": "支付相关：", "items": [{"record_id": "recA", "one_liner": "支付超时 P1"}]},
    ]
    h.service.handle(make_msg("/bug 查一下 支付超时 进展"))
    assert not h.api.records
    assert "支付" in h.api.replies[-1]["text"] or "recA" in h.api.replies[-1]["text"]


def test_followup_not_rerouted_to_query(harness):
    h = harness
    h.provider.outputs = [
        out(title=None, severity="P1", owner=None, due=None, proposer=None),
        out(title="补上的标题", severity="P1", owner=None, due=None, proposer=None),
    ]
    m = make_msg("/bug 有个线上问题")
    h.service.handle(m)
    ask = h.api.replies[-1]
    # even if the reply looks like a query, it must continue the follow-up
    h.service.handle(make_msg("这个进展怎样了 补上的标题", root_id=m.message_id, parent_id=ask["id"], at_bot=False))
    assert h.api.records and h.api.records[0]["fields"]["标题"] == "补上的标题"


def test_queryable_false_rejects(harness):
    h = harness
    h.service.cfg.find_role("/日报").queryable = False
    h.provider.outputs = [intent("/日报", 0.95, action="query", query="日报", keywords=["日报"])]
    h.service.handle(make_msg("我的日报进展怎样"))
    # /日报 not queryable → either pick among queryable (/bug) or reject
    text = h.api.replies[-1]["text"]
    assert "不支持查询" in text or "缺陷登记" in text or "哪张表" in text or "没找到" in text
