from pathlib import Path

import pytest

from bitable_bot.config import load_config
from bitable_bot.requirements_v09.writer import MultiTableWriter
from bitable_bot.session import Store
from bitable_bot.writer import BitableWriter
from tests.conftest import FakeAPI

CONFIG = Path(__file__).resolve().parents[2] / "config/roles.requirements.v09.yaml"


def test_config_and_all_table_mappings():
    cfg = load_config(CONFIG, env={})
    assert cfg.roles[0].target.table_id == cfg.tables.requirements.table_id
    writer = MultiTableWriter(BitableWriter(FakeAPI(), Store()), cfg.tables)
    for name, key, column in [
        ("requirements", "title", "需求标题"),
        ("tasks", "title", "任务标题"),
        ("progress", "summary", "进展摘要"),
        ("modules", "project", "项目名称"),
    ]:
        result = writer.write(name, {key: "示例"}, "m1", dry_run=True)
        assert result["fields"] == {column: "示例"}
    with pytest.raises(ValueError, match="undeclared"):
        writer.write("progress", {"secret": "no"}, "m1")


def test_idempotency_is_table_scoped():
    cfg = load_config(CONFIG, env={})
    api = FakeAPI()
    writer = MultiTableWriter(BitableWriter(api, Store()), cfg.tables)
    writer.write("requirements", {"title": "需求"}, "same")
    writer.write("tasks", {"title": "任务"}, "same")
    assert writer.write("requirements", {"title": "需求"}, "same").duplicated
    assert len(api.records) == 2
    assert api.records[0]["client_token"] != api.records[1]["client_token"]


def test_observation_requires_admin_permission_and_chat_scope():
    from pydantic import ValidationError

    cfg = load_config(CONFIG, env={}).model_dump()
    cfg["intake"] = {"observe_groups": True}
    with pytest.raises(ValidationError, match="群全部消息可读权限"):
        type(load_config(CONFIG)).model_validate(cfg)


def test_table_binding_is_single_source_and_cannot_disable_confirm():
    from bitable_bot.config.models import AppConfig

    raw = load_config(CONFIG, env={}).model_dump()
    raw["roles"][0]["target"] = {"app_token": "wrong", "table_id": "wrong"}
    raw["roles"][0]["confirm_before_write"] = False
    raw["tables"]["requirements"]["fields"][0]["field"] = "自定义需求标题"
    cfg = AppConfig.model_validate(raw)
    assert cfg.roles[0].target.table_id == cfg.tables.requirements.table_id
    assert cfg.roles[0].fields[0].field == "自定义需求标题"
    assert cfg.roles[0].confirm_before_write


def test_sdk_update_and_full_pagination():
    from types import SimpleNamespace

    from bitable_bot.writer.feishu_api import LarkFeishuAPI

    calls = []
    response = SimpleNamespace(success=lambda: True)
    endpoint = SimpleNamespace(update=lambda req: calls.append(req) or response)
    api = LarkFeishuAPI(SimpleNamespace(bitable=SimpleNamespace(v1=SimpleNamespace(app_table_record=endpoint))))
    api.update_record("base", "table", "rec1", {"进展摘要": "完成联调"})
    assert calls[0].record_id == "rec1" and calls[0].request_body.fields == {"进展摘要": "完成联调"}
    pages = iter(
        [
            {"items": [{"record_id": "a"}], "has_more": True, "page_token": "p2"},
            {"items": [{"record_id": "b"}], "has_more": False},
        ]
    )
    api._get_json = lambda *args: next(pages)
    assert len(api.list_all_records("base", "table")) == 2
