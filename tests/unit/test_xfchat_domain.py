import pytest

from bitable_bot.main import DOMAINS, ConfigError, Settings
from bitable_bot.requirements_v09.send_adapter import manager_delivery_items, recipient_target
from bitable_bot.requirements_v09.user_api import UserAPI


def test_settings_xfchat_domain():
    s = Settings({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b", "FEISHU_DOMAIN": "XFCHAT",
                  "FEISHU_WEB_DOMAIN": "https://t.xfchat.iflytek.com"})
    assert s.open_domain == "https://open.xfchat.iflytek.com"
    assert s.applink_host == ""
    assert DOMAINS["xfchat"][0] == s.open_domain


def test_settings_rejects_unknown_domain():
    with pytest.raises(ConfigError, match="xfchat"):
        Settings({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b", "FEISHU_DOMAIN": "nope"})


def test_user_api_accepts_xfchat():
    api = UserAPI("t", "https://open.xfchat.iflytek.com")
    assert api.domain == "https://open.xfchat.iflytek.com"
    with pytest.raises(ValueError):
        UserAPI("t", "https://evil.example.com")


@pytest.mark.parametrize(
    "recipient,expected",
    [
        ({"open_id": "ou_1", "email": "a@x"}, ("ou_1", "open_id")),
        ({"chat_id": "oc_1", "email": "a@x"}, ("oc_1", "chat_id")),
        ({"email": "a@x"}, ("a@x", "email")),
        ({}, ("", "open_id")),
    ],
)
def test_recipient_target(recipient, expected):
    assert recipient_target(recipient) == expected


def test_manager_items_email_without_contact_lookup():
    items = manager_delivery_items({"period": "2026-W45", "body": "x"}, [{"email": "xywang73@iflytek.com"}, {}])
    assert len(items) == 1
    assert items[0]["recipient"] == "xywang73@iflytek.com" and items[0]["receive_id_type"] == "email"


def test_web_domain_override():
    s = Settings({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b", "FEISHU_DOMAIN": "xfchat",
                  "FEISHU_WEB_DOMAIN": "https://yf2ljykclb.xfchat.iflytek.com/"})
    assert s.web_domain == "https://yf2ljykclb.xfchat.iflytek.com"


def test_xfchat_requires_web_domain():
    with pytest.raises(ConfigError, match="FEISHU_WEB_DOMAIN"):
        Settings({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b", "FEISHU_DOMAIN": "xfchat"})


def test_applink_override_and_omitted_link():
    from bitable_bot.context import build_message_link

    s = Settings({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b", "FEISHU_DOMAIN": "xfchat",
                  "FEISHU_WEB_DOMAIN": "https://t", "FEISHU_APPLINK_HOST": "applink.example"})
    assert s.applink_host == "applink.example"
    item = {"chat_id": "oc_1", "message_position": 3}
    assert build_message_link(item, "oc_1", "") == ""
    assert build_message_link({"message_app_link": "x"}, "oc_1", "") == "x"
