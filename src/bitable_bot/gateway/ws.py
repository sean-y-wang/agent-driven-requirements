"""Long-connection (WebSocket) gateway. Handlers return immediately; work runs in a thread pool."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog

from ..service import BotService
from .events import to_incoming

log = structlog.get_logger(__name__)


def _card_payload(data: Any) -> dict[str, Any]:
    """Normalize P2CardActionTrigger (object or dict) into a plain dict for BotService."""
    if isinstance(data, dict):
        event = data.get("event") or data
        return event if isinstance(event, dict) else {}
    event = getattr(data, "event", None)
    if event is None:
        return {}

    def dump(obj: Any) -> Any:
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, dict):
            return {k: dump(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [dump(x) for x in obj]
        # lark model objects expose public attrs
        out = {}
        for k, v in vars(obj).items() if hasattr(obj, "__dict__") else []:
            if k.startswith("_"):
                continue
            out[k] = dump(v)
        if out:
            return out
        for name in ("operator", "action", "context", "token", "host", "delivery_type"):
            if hasattr(obj, name):
                out[name] = dump(getattr(obj, name))
        return out

    return dump(event) or {}


class Gateway:
    def __init__(self, service: BotService, workers: int = 4) -> None:
        self.service = service
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bot")

    def on_message(self, data: Any) -> None:
        """Registered for im.message.receive_v1. Must return within 3 seconds."""
        try:
            msg = to_incoming(data)
        except Exception:
            log.exception("event.parse_failed")
            return
        log.info("event.received", event_id=msg.event_id, message_id=msg.message_id, chat_id=msg.chat_id)
        self.pool.submit(self.service.handle, msg)

    def on_card_action(self, data: Any) -> Any:
        """Registered for card.action.trigger (confirm / cancel / modify). Must return a response."""
        try:
            payload = _card_payload(data)
            log.info("card.action", value=(payload.get("action") or {}).get("value"))
            result = self.service.handle_card_action(payload)
        except Exception:
            log.exception("card.action_failed")
            result = {"toast": {"type": "error", "content": "处理失败，请稍后重试"}}
        # Build SDK response when available; otherwise return dict (tests / stubs).
        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )

            return P2CardActionTriggerResponse(result)
        except Exception:
            return result

    def on_bot_added(self, data: Any) -> None:  # optional event: im.chat.member.bot.added_v1
        return None

    def build_handler(self):
        import lark_oapi as lark

        return (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.on_message)
            .register_p2_card_action_trigger(self.on_card_action)
            .build()
        )

    def run(self, app_id: str, app_secret: str, domain: str, log_level: Any = None) -> None:
        import lark_oapi as lark

        client = lark.ws.Client(
            app_id,
            app_secret,
            event_handler=self.build_handler(),
            domain=domain,
            log_level=log_level or lark.LogLevel.INFO,
        )
        client.start()  # blocks, auto-reconnects
