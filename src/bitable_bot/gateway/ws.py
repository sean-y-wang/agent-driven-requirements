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

    CARD_WAIT_S = 0.6  # reply well under the platform's callback timeout

    @staticmethod
    def _pending_card(value: Any) -> dict[str, Any]:
        act = (value or {}).get("action") if isinstance(value, dict) else None
        label = {"confirm": "已确认，正在收录…", "cancel": "已取消", "modify": "请直接回复要修改的内容"}.get(
            act or "", "已收到，处理中…"
        )
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "⏳ " + label}, "template": "blue"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "结果稍后以消息回执通知。"}}],
        }
        return {"toast": {"type": "info", "content": label}, "card": {"type": "raw", "data": card}}

    def on_card_action(self, data: Any) -> Any:
        """card.action.trigger: reply fast; work runs in pool; duplicate callbacks are idempotent."""
        import json as _json
        import threading
        import time
        from concurrent.futures import TimeoutError as _Timeout

        t0 = time.monotonic()
        value = None
        try:
            payload = _card_payload(data)
            value = (payload.get("action") or {}).get("value")
            mid = ((payload.get("context") or {}).get("open_message_id")) or ""
            key = mid + "|" + _json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
            log.info("card.action", value=value)
            if not hasattr(self, "_card_futs"):
                self._card_futs, self._card_lock = {}, threading.Lock()
            with self._card_lock:
                fut = self._card_futs.get(key) if mid else None
                dup = fut is not None
                if fut is None:
                    fut = self.pool.submit(self.service.handle_card_action, payload)
                    if mid:
                        self._card_futs[key] = fut
                        if len(self._card_futs) > 500:
                            self._card_futs.pop(next(iter(self._card_futs)))
            try:
                result = fut.result(timeout=0 if dup else self.CARD_WAIT_S)
            except _Timeout:
                result = self._pending_card(value)
            if dup and not (isinstance(result, dict) and result.get("card")):
                result = self._pending_card(value) if not fut.done() else (result or {})
        except Exception:
            log.exception("card.action_failed")
            result = {"toast": {"type": "error", "content": "处理失败，请稍后重试"}}
        ms = int((time.monotonic() - t0) * 1000)
        log.info(
            "card.action_done",
            ms=ms,
            toast=(result or {}).get("toast", {}).get("type") if isinstance(result, dict) else None,
            has_card=isinstance(result, dict) and isinstance(result.get("card"), dict),
        )
        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )

            return P2CardActionTriggerResponse(result)
        except Exception:
            return result

    def on_bot_added(self, data: Any) -> None:  # optional event: im.chat.member.bot.added_v1
        try:
            chat_id = getattr(getattr(data, "event", None), "chat_id", "") or ""
            handler = getattr(self.service, "handle_bot_added", None)
            if handler:
                handler(chat_id)
        except Exception:
            return None
        return None

    def build_handler(self):
        import lark_oapi as lark

        return (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.on_message)
            .register_p2_card_action_trigger(self.on_card_action)
            .register_p2_im_chat_member_bot_added_v1(self.on_bot_added)
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
