"""Entrypoint: read env, load config, wire components, start the long connection."""

from __future__ import annotations

import os
import sys
import threading

import structlog

from .config import ConfigError, load_config
from .config.remote import apply_remote_config
from .context import ContextCollector
from .extractor import Extractor
from .logging_setup import add_log_hook, setup_logging
from .providers.openai_compat import build_providers
from .query import QueryService
from .requirements_v09.reports import ReportStore
from .service import BotService
from .session import Store
from .writer import BitableWriter, LarkFeishuAPI

log = structlog.get_logger(__name__)

DOMAINS = {
    "feishu": ("https://open.feishu.cn", "https://feishu.cn", "applink.feishu.cn"),
    "lark": ("https://open.larksuite.com", "https://larksuite.com", "applink.larksuite.com"),
}


# Built-in scenario id → shipped YAML (dedicated single-role bots).
BUILTIN_SCENARIOS = {
    "requirements": "config/roles.requirements.yaml",
}


def resolve_config_path(env: dict[str, str]) -> str:
    """BOT_CONFIG wins; else BOT_SCENARIO picks a built-in profile; else config/roles.yaml."""
    if env.get("BOT_CONFIG"):
        return env["BOT_CONFIG"]
    scenario = (env.get("BOT_SCENARIO") or "").strip().lower()
    if scenario:
        if scenario not in BUILTIN_SCENARIOS:
            known = ", ".join(sorted(BUILTIN_SCENARIOS))
            raise ConfigError(f"unknown BOT_SCENARIO={scenario!r}; known: {known}")
        return BUILTIN_SCENARIOS[scenario]
    return "config/roles.yaml"


class Settings:
    def __init__(self, env: dict[str, str]) -> None:
        missing = [k for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET") if not env.get(k)]
        if missing:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")
        self.app_id = env["FEISHU_APP_ID"]
        self.app_secret = env["FEISHU_APP_SECRET"]
        domain = env.get("FEISHU_DOMAIN", "feishu").lower()
        if domain not in DOMAINS:
            raise ConfigError("FEISHU_DOMAIN must be 'feishu' or 'lark'")
        self.open_domain, self.web_domain, self.applink_host = DOMAINS[domain]
        self.config_path = resolve_config_path(env)
        self.db_path = env.get("BOT_DB_PATH", "data/bot.db")
        self.report_db_path = env.get("REPORT_DB_PATH", "data/reports.db")
        self.bot_open_id = env.get("FEISHU_BOT_OPEN_ID") or None
        self.log_level = env.get("LOG_LEVEL", "INFO")
        self.workers = int(env.get("BOT_WORKERS", "4"))


def resolve_bot_open_id(api, configured: str | None) -> str | None:
    """Env override, else ask the platform (GET /open-apis/bot/v3/info, no extra scope needed)."""
    if configured:
        return configured
    try:
        return api.get_bot_open_id() or None
    except Exception as e:  # network / permission: fall back to the leading-mention heuristic
        log.warning("bot_info.unavailable", error=str(e)[:200])
        return None


def build_service(settings: Settings, cfg, lark_client) -> BotService:
    store = Store(settings.db_path)
    api = LarkFeishuAPI(lark_client)
    cfg = apply_remote_config(cfg, api)
    if cfg.intake.observe_groups:
        for chat_id in cfg.intake.observed_chats:
            try:
                api.list_messages("chat", chat_id, 1)
            except Exception as e:
                raise ConfigError(
                    "群消息读取预检失败：请管理员开通群全部消息可读权限、发布应用并确认机器人在目标群中。"
                ) from e
    providers = build_providers(cfg)
    bot_id = resolve_bot_open_id(api, settings.bot_open_id)
    collector = ContextCollector(api, cfg, applink_host=settings.applink_host, bot_open_id=bot_id)
    extractor = Extractor(providers, cfg.llm.default, fallback=cfg.llm.fallback)
    qsvc = QueryService(api, cfg, providers, cfg.llm.default, base_url=settings.web_domain)
    return BotService(
        cfg,
        api,
        store,
        extractor,
        BitableWriter(api, store, base_url=settings.web_domain),
        bot_open_id=bot_id,
        collector=collector,
        query_service=qsvc,
        report_store=ReportStore(settings.report_db_path) if cfg.tables else None,
    )


def _sweeper(service: BotService, interval: float = 60.0) -> None:
    stop = threading.Event()
    while not stop.wait(interval):
        try:
            n = service.expire_sessions()
            service.store.purge_expired()
            if n:
                log.info("session.expired", count=n)
        except Exception:
            log.exception("sweeper.failed")


def start_dashboard(settings: Settings, service: BotService, env: dict[str, str]):
    """Start the in-process dashboard + business-config auto refresh. Returns Runtime (or None)."""
    import logging

    from .dashboard import DashboardServer, Runtime, dashboard_settings

    runtime = Runtime(settings, service, service.cfg, env_path=env.get("BOT_ENV_FILE", ".env"))
    add_log_hook(runtime.structlog_processor)
    logging.getLogger("Lark").addHandler(runtime.ws_log_handler())
    runtime.start_auto_refresh()
    try:
        ds = dashboard_settings(env)
    except ValueError as e:
        log.error("dashboard.disabled", reason=str(e))
        return runtime
    if ds is None:
        log.info("dashboard.disabled", reason="DASHBOARD_TOKEN not set")
        return runtime
    try:
        DashboardServer(runtime, ds["host"], ds["port"], ds["token"]).start()
    except OSError as e:
        log.error("dashboard.bind_failed", host=ds["host"], port=ds["port"], error=str(e))
    return runtime


def main() -> int:
    try:
        settings = Settings(dict(os.environ))
        setup_logging(settings.log_level)
        cfg = load_config(settings.config_path)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    import lark_oapi as lark

    from .gateway import Gateway

    lark_client = (
        lark.Client.builder()
        .app_id(settings.app_id)
        .app_secret(settings.app_secret)
        .domain(settings.open_domain)
        .build()
    )
    service = build_service(settings, cfg, lark_client)
    threading.Thread(target=_sweeper, args=(service,), daemon=True, name="sweeper").start()
    start_dashboard(settings, service, dict(os.environ))
    log.info(
        "bot.starting",
        scenario=cfg.scenario,
        dedicated=cfg.dedicated,
        bot_name=cfg.display_name(),
        roles=[r.command for r in cfg.roles],
        db=settings.db_path,
        bot_open_id=service.bot_open_id,
    )
    Gateway(service, settings.workers).run(settings.app_id, settings.app_secret, settings.open_domain)
    return 0


if __name__ == "__main__":
    sys.exit(main())
