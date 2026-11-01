"""One-report card interaction, invoked only after Feishu's verified WS event handler."""

from ..requirements_v09.reports import confirmation_card


def editor_card(draft):
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "修改本份汇报摘要"}},
        "elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content": draft["body"]}},
            {
                "tag": "form",
                "name": "report_edit",
                "elements": [
                    {
                        "tag": "input",
                        "name": "summary",
                        "required": True,
                        "placeholder": {"tag": "plain_text", "content": "输入完整新摘要；保存后再次确认"},
                    },
                    {
                        "tag": "button",
                        "name": "save_report",
                        "action_type": "form_submit",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "保存摘要"},
                        "value": {
                            "kind": "report_confirm",
                            "action": "edit",
                            "report_id": draft["id"],
                            "revision": draft["revision"],
                        },
                    },
                ],
            },
        ],
    }


def handle_report_action(store, payload, today):
    try:
        if store is None:
            raise ValueError("汇报功能未启用")
        action = payload.get("action") or {}
        value = action.get("value") or {}
        operator = (payload.get("operator") or {}).get("open_id", "")
        report_id, revision = value.get("report_id", ""), value.get("revision", "")
        op = value.get("action", "confirm")  # 0.9 cards remain accepted.
        if op == "edit" and not action.get("form_value"):
            draft = store.get(report_id)
            if not store.valid(draft, revision, operator, today):
                raise ValueError("确认已过期、已处理或非本人操作")
            return {
                "toast": {"type": "info", "content": "请填写新摘要，保存后再确认"},
                "card": {"type": "raw", "data": editor_card(draft)},
            }
        draft = store.act(
            report_id,
            revision,
            operator,
            today,
            op,
            body=(action.get("form_value") or {}).get("summary") if op == "edit" else None,
        )
        if op == "edit":
            card = confirmation_card(draft)
            message = "摘要已保存，请重新确认"
        else:
            message = "汇报已确认" if op == "confirm" else "汇报已作废"
            card = {"elements": [{"tag": "div", "text": {"tag": "plain_text", "content": message}}]}
        return {"toast": {"type": "success", "content": message}, "card": {"type": "raw", "data": card}}
    except (ValueError, TypeError, AttributeError):
        return {"toast": {"type": "error", "content": "操作无效、已过期、已处理或非本人操作"}}
