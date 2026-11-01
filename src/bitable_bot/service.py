"""Business pipeline.

trigger (@bot in group / any p2p message / reply in an open follow-up thread)
→ optional emoji reaction (dedicated bots)
→ route (slash = explicit role; otherwise LLM intent with action=record|query)
→ dedicated mode: single role, only record|query|help (no role picking)
→ short-content guide / progress query / extract → validate
→ follow-up / confirm-before-write (interactive card) / write → receipt → audit.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from .config.models import AppConfig, Role
from .context import ContextCollector, ConversationContext
from .edit_fields import is_cancel, is_skip, is_yes, parse_edits
from .extractor import ExtractionError, Extractor, build_messages, classify
from .extractor.intent import looks_like_help, looks_like_query
from .query import QueryService
from .receipt import confirm_card, followup_text, preview_lines, query_card, success_card, success_text
from .requirements_v09.writer import MultiTableWriter
from .router import IncomingMessage, Kind, bot_mentioned, clean_text, extract_text, help_text, route
from .session import Session, Store
from .validator import Context, validate
from .writer import BitableWriter, FeishuAPI, FeishuAPIError, WriteError

log = structlog.get_logger(__name__)

PICK = "__pick__"
PHASE_CONFIRM = "confirm"
PHASE_MODIFY = "modify"  # waiting for free-text field edits after card 「修改」
PHASE_GUIDE = "guide"  # unused for session (guide does not open a session)
_HIDDEN_IN_RECEIPT = {"$context", "$raw_text"}


def _auto_guide(role: Role) -> str:
    req = "、".join(
        f.field for f in role.fields if f.required and not (isinstance(f.default, str) and f.default.startswith("$"))
    )
    style = f"（{role.reply_style}）" if role.reply_style else ""
    base = role.guide_prompt or (f"想记录到「{role.name}」的话，请尽量说清：{req or '关键内容'}。例：{role.command} ……")
    return base.strip() + (f"\n{style}" if style and role.guide_prompt is None else "")


def _is_thin(text: str, excerpt: str, min_chars: int) -> bool:
    if min_chars <= 0:
        return False
    body = (text or "").strip()
    ctx = (excerpt or "").strip()
    return len(body) < min_chars and len(ctx) < min_chars


def _record_too_vague(role: Role, ext_fields: dict, raw_text: str) -> bool:
    """True when extract looks like chitchat-as-record: no title / almost empty + short text."""
    title = ext_fields.get("title")
    if title not in (None, "", []):
        return False
    body = (raw_text or "").strip()
    min_chars = role.min_content_chars or 12
    if len(body) < min_chars:
        return True
    # Count non-empty LLM keys excluding ones that are usually defaults
    skip = {
        "project",
        "type",
        "priority",
        "status",
        "proposer",
        "source",
        "raw_text",
        "context",
        "message_link",
        "submitted_at",
    }
    filled = [k for k, v in ext_fields.items() if v not in (None, "", []) and k not in skip]
    return len(filled) == 0


class BotService:
    def __init__(
        self,
        cfg: AppConfig,
        api: FeishuAPI,
        store: Store,
        extractor: Extractor,
        writer: BitableWriter,
        bot_open_id: str | None = None,
        clock=time.time,
        collector: ContextCollector | None = None,
        query_service: QueryService | None = None,
        report_store=None,
    ) -> None:
        self.cfg = cfg
        self.api = api
        self.store = store
        self.extractor = extractor
        self.writer = writer
        self.bot_open_id = bot_open_id
        self.clock = clock
        self.collector = collector or ContextCollector(api, cfg, bot_open_id=bot_open_id, clock=clock)
        self.query = query_service
        self.report_store = report_store

    # ------------------------------------------------------------------ entry
    def is_duplicate(self, msg: IncomingMessage) -> bool:
        dup_event = bool(msg.event_id) and self.store.seen(f"evt:{msg.event_id}")
        dup_msg = self.store.seen(f"msg:{msg.message_id}")
        return dup_event or dup_msg

    def handle(self, msg: IncomingMessage) -> None:
        try:
            # Lazy cleanup so timeouts fire even if the background sweeper is delayed.
            self.expire_sessions()
            self._handle(msg)
        except Exception:
            log.exception("handle.failed", message_id=msg.message_id)
            self._reply(msg.message_id, "处理消息时出现内部错误，请稍后重试。")

    def _thread_session(self, msg: IncomingMessage, sessions: list[Session]) -> Session | None:
        for s in sessions:
            if (msg.root_id and msg.root_id == s.source_message_id) or (
                msg.parent_id and (msg.parent_id in s.ask_message_ids or msg.parent_id == s.source_message_id)
            ):
                return s
        return None

    def _maybe_first_guide(self, msg: IncomingMessage) -> None:
        if not self.cfg.first_guide_enabled or msg.chat_type == "p2p":
            return
        if self.store.mark_chat_guided(msg.chat_id):
            self._reply(msg.message_id, self.cfg.first_guide_text)

    def _handle(self, msg: IncomingMessage) -> None:
        if msg.sender_type != "user":
            return
        if self.is_duplicate(msg):
            log.info("dedup.skip", event_id=msg.event_id, message_id=msg.message_id)
            return
        p2p = msg.chat_type == "p2p"
        if p2p and not self.cfg.p2p_enabled:
            return
        raw = extract_text(msg.message_type, msg.content)
        sessions = self.store.sessions_for(msg.chat_id, msg.sender_open_id)
        thread_s = self._thread_session(msg, sessions)
        addressed = p2p or bot_mentioned(msg.mentions, raw, self.bot_open_id)
        if not addressed and thread_s is None:
            if (self.cfg.tables and self.cfg.intake.observe_groups and msg.chat_type == "group"
                    and msg.chat_id in self.cfg.intake.observed_chats
                    and (not self.cfg.chats_allowlist or msg.chat_id in self.cfg.chats_allowlist)
                    and msg.message_type in ("text", "post") and msg.sender_open_id
                    and msg.sender_open_id != self.bot_open_id):
                text, people = clean_text(raw, msg.mentions, self.bot_open_id, strip_leading=False)
                self._detect(msg, text, people, passive=True)
            return
        if msg.message_type not in ("text", "post"):
            self._reply(msg.message_id, "暂只支持文字消息。发送 /help 查看用法。")
            return
        if self.cfg.chats_allowlist and not p2p and msg.chat_id not in self.cfg.chats_allowlist:
            self._reply(msg.message_id, "抱歉，本群未开通记录功能，请联系管理员。")
            return
        text, people = clean_text(raw, msg.mentions, self.bot_open_id, strip_leading=not p2p)
        # Fresh @ / p2p (not a thread follow-up): optional 「收到」reaction
        if addressed and thread_s is None and self.cfg.react_on_mention:
            self._react(msg.message_id)
        r = route(text, self.cfg)
        if r.kind is Kind.HELP:
            self._maybe_first_guide(msg)
            self._reply(msg.message_id, help_text(self.cfg, msg.chat_id))
        elif r.kind is Kind.CANCEL:
            for s in sessions:
                self.store.delete_session(s)
            self._reply(msg.message_id, "已取消未完成的记录。" if sessions else "当前没有未完成的记录。")
        elif r.kind is Kind.UNKNOWN:
            self._reply(msg.message_id, f"未知命令 {r.command}。\n" + help_text(self.cfg, msg.chat_id))
        elif r.kind is Kind.ROLE:
            assert r.role is not None
            self._maybe_first_guide(msg)
            # slash + query hints → progress query (no session)
            if r.role.queryable and r.body and looks_like_query(r.body, self.cfg.intent.query_hints):
                self._run_query(msg, r.role, r.body, people)
                return
            self._start(msg, r.role, r.body, people, raw_text=text)
        else:
            s = thread_s or (sessions[0] if sessions else None)
            if self.cfg.tables and s and not thread_s and msg.chat_type == "group":
                role = self.cfg.find_role(s.role)
                # A fresh group message must not become an edit of an unrelated pending draft.
                if role and not (is_yes(text) or is_cancel(text) or is_skip(text) or parse_edits(role, text)):
                    self._detect(msg, text, people)
                    return
            if s is None:
                self._maybe_first_guide(msg)
                self._detect(msg, text, people)
            elif s.role == PICK:
                self._continue_pick(msg, s, text, people)
            elif s.raw_fields.get("__phase__") == PHASE_CONFIRM:
                self._continue_confirm(msg, s, text, people)
            elif s.raw_fields.get("__phase__") == PHASE_MODIFY:
                self._continue_modify(msg, s, text, people)
            else:
                # follow-up / confirm answers never branch into query
                self._continue_followup(msg, s, text, people)

    # ------------------------------------------------------------------ intent
    def _permitted(self, msg: IncomingMessage, role: Role) -> bool:
        if role.allowed_chats and msg.chat_id not in role.allowed_chats:
            return False
        return not role.allowed_users or msg.sender_open_id in role.allowed_users

    def _detect(self, msg: IncomingMessage, text: str, people: dict[str, str], passive: bool = False) -> None:
        if not self.cfg.intent.enabled:
            if passive:
                return
            self._reply(msg.message_id, "请使用命令开头，例如：\n" + help_text(self.cfg, msg.chat_id))
            return
        roles = [r for r in self.cfg.roles if self._permitted(msg, r)]
        if not roles:
            if passive:
                return
            self._reply(msg.message_id, "抱歉，你在这里没有可用的记录角色。")
            return
        # Dedicated single-role bot (e.g. 需求小助手): fixed role, only record|query.
        dedicated_role = self.cfg.primary_role() if self.cfg.dedicated else None
        if dedicated_role is not None and dedicated_role in roles:
            roles = [dedicated_role]
        conv = ConversationContext() if passive else self.collector.collect(msg)
        if not text and not conv.excerpt:
            if passive:
                return
            # try single queryable/record role guide, else help
            if len(roles) == 1:
                self._reply(msg.message_id, _auto_guide(roles[0]))
            else:
                self._reply(msg.message_id, "想记录什么？直接说内容即可，例如：\n" + help_text(self.cfg, msg.chat_id))
            return
        provider = self.extractor.resolve(self.cfg.intent.llm)
        intent = classify(provider, self.cfg, roles, text, conv.excerpt)
        if self.cfg.tables:
            # No optimistic record fallback when classification fails in 0.9.
            if intent.confidence < self.cfg.intent.min_confidence or not intent.action:
                if not passive:
                    self._reply(msg.message_id, help_text(self.cfg, msg.chat_id))
                return
            role = dedicated_role or intent.role
            if role and intent.action == "progress":
                if msg.chat_type == "group":
                    self._group_progress(msg, text, role)
                elif not passive:
                    self._reply(msg.message_id, "个人研发进展请使用本机 progress-sync；也可查询需求或明确提需。")
                return
            if role and intent.action == "candidate":
                self._start(msg, role, text, people, raw_text=text, conv=conv, intake="candidate")
                return
            if passive and intent.action != "record":
                return
        # Force the dedicated role onto the intent when present (except help/chitchat).
        # Keep LLM confidence as-is for record/query so low-confidence can fall back to guide.
        if dedicated_role is not None:
            if intent.action == "help" or (intent.action not in ("record", "query", "help") and looks_like_help(text)):
                intent.action = "help"
                intent.role = None
                intent.confidence = max(intent.confidence, 0.9)
            else:
                intent.role = dedicated_role
                if intent.action not in ("record", "query"):
                    # Heuristic fallback when LLM returns null action
                    intent.action = "query" if looks_like_query(text, self.cfg.intent.query_hints) else "record"
                    intent.confidence = max(intent.confidence, 0.8)
        log.info(
            "intent.result",
            action=intent.action,
            role=intent.role.command if intent.role else None,
            confidence=intent.confidence,
            reason=intent.reason,
            dedicated=bool(dedicated_role),
        )

        if intent.action == "help":
            log.info("help.reply")
            self._reply(msg.message_id, help_text(self.cfg, msg.chat_id))
            return

        if intent.action == "query":
            self._handle_query_intent(msg, intent, roles, text, people, conv)
            return

        # Low-confidence / thin "like a requirement but not": guide, do not enter补字段.
        if dedicated_role is not None and (
            intent.confidence < self.cfg.intent.min_confidence
            or (dedicated_role.min_content_chars > 0 and _is_thin(text, conv.excerpt, dedicated_role.min_content_chars))
        ):
            log.info("intent.low_conf_guide", confidence=intent.confidence, text_len=len(text or ""))
            self._reply(msg.message_id, help_text(self.cfg, msg.chat_id))
            return

        if intent.role is not None and intent.confidence >= self.cfg.intent.min_confidence:
            self._start(msg, intent.role, text, people, raw_text=text, conv=conv)
            return
        # Dedicated bot never opens a role-pick session; unclear → guide.
        if dedicated_role is not None:
            self._reply(msg.message_id, help_text(self.cfg, msg.chat_id))
            return
        s = Session(
            chat_id=msg.chat_id,
            user_id=msg.sender_open_id,
            role=PICK,
            source_message_id=msg.message_id,
            text=text,
            raw_fields={
                "__ctx__": conv.to_dict(),
                "__people__": people,
                "__raw__": text,
                "__candidate__": intent.role.command if intent.role else "",
                "__action__": intent.action or "record",
                "__query__": intent.query,
                "__keywords__": intent.keywords,
            },
            expires_at=self.clock() + self.cfg.session.timeout_minutes * 60,
        )
        self._ask_pick(msg.message_id, s, roles, intent.role, for_query=(intent.action == "query"))

    def _group_progress(self, msg: IncomingMessage, text: str, role: Role) -> None:
        if not self._permitted(msg, role):
            return
        values = {
            "summary": text[:1000], "author": [{"id": msg.sender_open_id}],
            "day": datetime.fromtimestamp(self.clock(), ZoneInfo(self.cfg.timezone)).date().isoformat(),
            "agent": "群", "review": True, "sync_key": f"group:{msg.message_id}",
            "source_message": msg.message_id,
            "risk": text[:300] if any(word in text for word in ("阻塞", "风险", "延期", "卡住")) else "",
        }
        for key, value in self.cfg.resolve_chat_bindings(msg.chat_id, role):
            if key == "project":
                values["module"] = value
        try:
            wr = MultiTableWriter(self.writer, self.cfg.tables).write("progress", values, msg.message_id)
            self.store.audit(chat_id=msg.chat_id, sender=msg.sender_open_id,
                             command="progress", fields=values, record_id=wr.record_id, result="progress:review")
        except WriteError as e:
            self._reply(msg.message_id, e.user_message)

    def _handle_query_intent(self, msg, intent, roles, text, people, conv) -> None:
        qroles = [r for r in roles if r.queryable]
        role = intent.role if intent.role and intent.role.queryable else None
        if role is None and len(qroles) == 1:
            role = qroles[0]
        if role is not None and intent.confidence >= self.cfg.intent.min_confidence:
            self._run_query(msg, role, intent.query or text, people, keywords=intent.keywords)
            return
        if not qroles:
            self._reply(msg.message_id, "当前没有支持进展查询的角色。你可以直接描述新需求让我登记。")
            return
        # unclear which table: PICK with __action__=query (no write)
        s = Session(
            chat_id=msg.chat_id,
            user_id=msg.sender_open_id,
            role=PICK,
            source_message_id=msg.message_id,
            text=text,
            raw_fields={
                "__ctx__": conv.to_dict(),
                "__people__": people,
                "__raw__": text,
                "__candidate__": role.command if role else "",
                "__action__": "query",
                "__query__": intent.query or text,
                "__keywords__": intent.keywords,
            },
            expires_at=self.clock() + self.cfg.session.timeout_minutes * 60,
        )
        self._ask_pick(msg.message_id, s, qroles, role, for_query=True)

    def _ask_pick(
        self,
        reply_to: str,
        s: Session,
        roles: list[Role],
        guess: Role | None,
        retry: bool = False,
        for_query: bool = False,
    ) -> None:
        if for_query:
            head = (
                "没识别出要查哪张表。"
                if retry
                else (f"要在「{guess.name}」里查进展，对吗？" if guess else "要查询哪张表的进展？")
            )
            tail = "回复“是”确认，或回复角色名/命令；/取消 放弃。" if guess else "请回复角色名或命令；/取消 放弃。"
        else:
            head = (
                "没识别出你选的角色。"
                if retry
                else (f"我猜你想记录到「{guess.name}」，对吗？" if guess else "我不确定要记录到哪张表。")
            )
            tail = "回复“是”确认，或回复角色名/命令；/取消 放弃。" if guess else "请回复角色名或命令；/取消 放弃。"
        opts = "\n".join(f"• {r.name}（{r.command}）" for r in roles)
        ask_id = self._reply(reply_to, f"{head}\n可选：\n{opts}\n{tail}", thread=True)
        if ask_id:
            s.ask_message_ids.append(ask_id)
        self.store.save_session(s)

    def _continue_pick(self, msg: IncomingMessage, s: Session, text: str, people: dict[str, str]) -> None:
        role = self.cfg.match_role(text)
        cand = self.cfg.find_role(s.raw_fields.get("__candidate__") or "")
        if role is None and cand is not None and is_yes(text):
            role = cand
        for_query = s.raw_fields.get("__action__") == "query"
        roles = [r for r in self.cfg.roles if self._permitted(msg, r) and (r.queryable if for_query else True)]
        if role is None or role not in roles:
            s.followups.append(text)
            if len(s.followups) > self.cfg.session.max_followups:
                self.store.delete_session(s)
                self._reply(s.source_message_id, "未能确定角色，已放弃。可用 /help 查看命令后重新发送。", thread=True)
                return
            self._ask_pick(s.source_message_id, s, roles, cand, retry=True, for_query=for_query)
            return
        self.store.delete_session(s)
        if for_query:
            self._run_query(
                msg,
                role,
                s.raw_fields.get("__query__") or s.text,
                people,
                keywords=s.raw_fields.get("__keywords__") or [],
                reply_to=s.source_message_id,
            )
            return
        conv = ConversationContext.from_dict(s.raw_fields.get("__ctx__"))
        self._start(
            msg,
            role,
            s.text,
            {**s.raw_fields.get("__people__", {}), **people},
            raw_text=s.raw_fields.get("__raw__", s.text),
            conv=conv,
            source_id=s.source_message_id,
        )

    # ------------------------------------------------------------------ query
    def _run_query(
        self,
        msg: IncomingMessage,
        role: Role,
        query_text: str,
        people: dict[str, str],
        keywords: list[str] | None = None,
        reply_to: str | None = None,
    ) -> None:
        reply_to = reply_to or msg.message_id
        if not self._permitted(msg, role):
            self._reply(reply_to, f"抱歉，你在这里没有查询「{role.name}」的权限。")
            return
        if not role.queryable:
            self._reply(reply_to, f"「{role.name}」暂不支持查询进展。")
            return
        if self.query is None:
            self._reply(reply_to, "查询功能暂未就绪，请稍后再试或直接登记新内容。")
            return
        sender_name = self._sender_name(msg)
        t0 = time.monotonic()
        summary = ""
        try:
            qres = self.query.run(role, query_text, keywords, sender_name=sender_name)
            text, hits, rows, summary = qres.text, qres.record_ids, qres.rows, qres.summary
            result = "ok" if hits else "empty"
        except Exception as e:
            log.exception("query.failed")
            text, hits, rows, result = f"查询暂时失败：{e}", [], [], "error"
        self.store.audit(
            chat_id=msg.chat_id,
            sender=msg.sender_open_id,
            command=f"query:{role.command}",
            text=query_text if self.cfg.audit_store_text else None,
            latency_ms=int((time.monotonic() - t0) * 1000),
            fields={"hits": hits},
            result=result,
        )
        docs = self.cfg.query_docs_hint
        if hits:
            card = query_card(
                role,
                summary or text.split("\n", 1)[0],
                rows,
                docs_hint=docs,
                base_url=getattr(self.query, "base_url", "https://feishu.cn"),
            )
            if self._reply_card(reply_to, card):
                return
            if docs:
                text = text.rstrip() + "\n" + docs
        elif docs and result == "empty":
            pass
        self._reply(reply_to, text)

    # ------------------------------------------------------------------ extraction flow
    def _start(
        self,
        msg: IncomingMessage,
        role: Role,
        body: str,
        people: dict[str, str],
        *,
        raw_text: str,
        conv: ConversationContext | None = None,
        source_id: str | None = None,
        intake: str = "record",
    ) -> None:
        source_id = source_id or msg.message_id
        if not self._permitted(msg, role):
            self._reply(source_id, f"抱歉，你在这里没有使用「{role.name}」的权限。")
            return
        lookup_key = MultiTableWriter(self.writer, self.cfg.tables).key("requirements", source_id) if self.cfg.tables else source_id
        existing = self.store.get_record(lookup_key)
        if existing:
            if intake != "candidate":
                self._reply_duplicate(source_id, role, existing[1])
            return
        if conv is None:
            conv = self.collector.collect(msg)
        if intake != "candidate" and role.min_content_chars > 0 and _is_thin(body, conv.excerpt, role.min_content_chars):
            tip = help_text(self.cfg, msg.chat_id) if self.cfg.dedicated else _auto_guide(role)
            self._reply(source_id, tip)
            return
        if not body and not conv.excerpt:
            req = "、".join(f.field for f in role.fields if f.required)
            self._reply(source_id, f"请在命令后附上内容，例如：{role.command} ……（需要包含：{req}）")
            return
        for old in self.store.sessions_for(msg.chat_id, msg.sender_open_id):
            if intake != "candidate" and old.role in (role.command, PICK):
                self.store.delete_session(old)
        s = Session(
            chat_id=msg.chat_id,
            user_id=msg.sender_open_id,
            role=role.command,
            source_message_id=source_id,
            text=body,
            raw_fields={"__ctx__": conv.to_dict(), "__people__": people, "__raw__": raw_text, "__intake__": intake, "__chat_type__": msg.chat_type},
        )
        self._process(msg, role, s, people)

    def _continue_followup(self, msg: IncomingMessage, s: Session, text: str, people: dict[str, str]) -> None:
        role = self.cfg.find_role(s.role)
        if role is None:
            self.store.delete_session(s)
            return
        if not text:
            self._reply(msg.message_id, "请直接回复需要补充的内容。", thread=True)
            return
        if is_skip(text):
            # optional fields are never required; skip just re-runs validate on current values
            s.raw_fields["__skipped_optional__"] = True
            self._process(msg, role, s, people, skip_extract=True)
            return
        s.followups.append(text)
        self._process(msg, role, s, people)

    def _continue_confirm(self, msg: IncomingMessage, s: Session, text: str, people: dict[str, str]) -> None:
        role = self.cfg.find_role(s.role)
        if role is None:
            self.store.delete_session(s)
            return
        if is_cancel(text):
            self.store.delete_session(s)
            self._reply(s.source_message_id, "已取消，未写入。", thread=True)
            return
        if is_yes(text):
            s.raw_fields["__confirmed__"] = True
            self._process(msg, role, s, people, skip_extract=True)
            return
        edits = parse_edits(role, text)
        if edits:
            merged = {k: v for k, v in s.raw_fields.items() if not k.startswith("__")}
            merged.update(edits)
            for k, v in merged.items():
                if not k.startswith("__"):
                    s.raw_fields[k] = v
            # drop confirm flag so we re-preview
            s.raw_fields.pop("__confirmed__", None)
            s.raw_fields["__phase__"] = PHASE_CONFIRM
            self._process(msg, role, s, people, skip_extract=True, force_preview=True)
            return
        # free-text tweak: treat as follow-up extract then re-preview
        s.followups.append(text)
        s.raw_fields.pop("__confirmed__", None)
        self._process(msg, role, s, people, force_preview=True)

    def _context(
        self, msg: IncomingMessage, all_people: dict[str, str], conv: ConversationContext, raw_text: str
    ) -> Context:
        def lookup(name: str) -> list[str]:
            if msg.chat_type == "p2p":
                return []
            return [oid for oid, n in self.collector.chat_members(msg.chat_id).items() if n == name]

        return Context(
            sender_open_id=msg.sender_open_id,
            now=datetime.fromtimestamp(self.clock(), UTC),
            tz=self.cfg.timezone,
            sender_name=conv.sender_name,
            chat_name=conv.chat_name,
            message_link=conv.message_link,
            raw_text=raw_text,
            context_text=conv.excerpt,
            mentions=all_people,
            member_lookup=lookup,
        )

    def _process(
        self,
        msg: IncomingMessage,
        role: Role,
        s: Session,
        people: dict[str, str],
        skip_extract: bool = False,
        force_preview: bool = False,
    ) -> None:
        reply_to = s.source_message_id
        candidate = self.cfg.tables is not None and s.raw_fields.get("__intake__") == "candidate"
        all_people = {**s.raw_fields.get("__people__", {}), **people}
        conv = ConversationContext.from_dict(s.raw_fields.get("__ctx__"))
        raw_text = s.raw_fields.get("__raw__", s.text)
        ctx = self._context(msg, all_people, conv, raw_text)
        provider = self.extractor.resolve(role.llm)
        prev = {k: v for k, v in s.raw_fields.items() if not k.startswith("__")}
        audit = dict(
            chat_id=msg.chat_id,
            sender=msg.sender_open_id,
            command=role.command,
            text=raw_text if self.cfg.audit_store_text else None,
            provider=getattr(provider, "name", None),
            model=getattr(provider, "model", None),
        )

        if skip_extract:
            ext_fields = dict(prev)
            latency = 0
        else:
            messages = build_messages(
                role,
                s.text,
                now=ctx.now,
                tz=ctx.tz,
                sender_name=conv.sender_name,
                mentions=all_people,
                previous=prev or None,
                followups=s.followups,
                context=conv.excerpt,
            )
            t0 = time.monotonic()
            done = threading.Event()
            ack_timer = None
            if self.cfg.ack_after_seconds > 0 and not candidate:

                def _ack():
                    if not done.is_set():
                        self._reply(reply_to, "收到，正在整理…", thread=True)

                ack_timer = threading.Timer(self.cfg.ack_after_seconds, _ack)
                ack_timer.daemon = True
                ack_timer.start()
            try:
                ext = self.extractor.extract(role, messages)
            except ExtractionError as e:
                log.error("extract.failed", error=str(e))
                self.store.audit(**audit, latency_ms=int((time.monotonic() - t0) * 1000), result=f"llm_error:{e}"[:200])
                if s.followups:
                    s.followups.pop()
                if candidate:
                    return
                self._reply(reply_to, "AI 字段提取暂时失败，请稍后在此话题中回复重试，或 /取消。", thread=True)
                return
            finally:
                done.set()
                if ack_timer is not None:
                    ack_timer.cancel()
            latency = int((time.monotonic() - t0) * 1000)
            audit["latency_ms"] = latency
            ext_fields = dict(prev)
            ext_fields.update({k: v for k, v in ext.fields.items() if v not in (None, "", [])})

        # Group → field binding (e.g. 所属项目); show on confirm card; user may still edit.
        for key, val in self.cfg.resolve_chat_bindings(msg.chat_id, role):
            ext_fields[key] = val
            log.info("chat_binding.apply", chat_id=msg.chat_id, field_key=key, value=val)

        if self.cfg.tables:
            ext_fields.update(status="待分诊" if candidate else "待评审", review=bool(candidate),
                              agent="群" if s.raw_fields.get("__chat_type__", msg.chat_type) == "group" else "其他")
        result = validate(role.fields, ext_fields, ctx)
        if candidate:
            if not result.complete:
                self.store.audit(**audit, fields=result.values, result="candidate:incomplete")
                return
            try:
                wr = MultiTableWriter(self.writer, self.cfg.tables).write("requirements", result.values, s.source_message_id)
                self.store.audit(**audit, fields=result.values, record_id=wr.record_id, result="candidate:ok")
            except WriteError as e:
                self.store.audit(**audit, result=f"candidate:write_error:{e.code}")
                self._reply(reply_to, e.user_message)
            return

        if result.complete or (s.raw_fields.get("__skipped_optional__") and not result.missing):
            # confirm-before-write
            need_confirm = (role.confirm_before_write or self.cfg.tables is not None) and not s.raw_fields.get("__confirmed__")
            if need_confirm or force_preview:
                self._ask_confirm(reply_to, role, s, result.values, all_people, ctx.tz, audit)
                return
            self._do_write(msg, role, s, result.values, all_people, ctx, audit, reply_to)
            return

        # First-pass extract almost empty + short text → guide, not补字段连环追问.
        if (
            self.cfg.dedicated
            and not s.followups
            and not skip_extract
            and not force_preview
            and _record_too_vague(role, ext_fields, raw_text)
        ):
            self.store.audit(**audit, fields=result.values, result="guide:vague_extract")
            tip = help_text(self.cfg, msg.chat_id)
            self._reply(reply_to, tip)
            return

        if len(s.followups) >= self.cfg.session.max_followups:
            self.store.delete_session(s)
            self.store.audit(**audit, fields=result.values, result="abandoned:max_followups")
            names = "、".join(f.field for f in role.fields if f.key in result.missing)
            self._reply(
                reply_to,
                f"仍缺少必填字段：{names}，已放弃本次记录，未写入。请补全后重新发送 {role.command}。",
                thread=True,
            )
            return

        optional_empty = [
            f
            for f in role.fields
            if not f.required
            and f.key not in result.values
            and not (isinstance(f.default, str) and f.default.startswith("$"))
        ]
        s.raw_fields = {
            **{k: v for k, v in s.raw_fields.items() if k.startswith("__")},
            **{k: v for k, v in ext_fields.items()},
            **{k: result.values[k] for k in result.values},
            "__people__": all_people,
            "__ctx__": conv.to_dict(),
            "__raw__": raw_text,
        }
        # keep raw extracted keys for next merge; overwrite with validated where present
        for k, v in result.values.items():
            s.raw_fields[k] = v if not isinstance(v, (dict, list)) else ext_fields.get(k, v)
        # store validated-ready values under keys for confirm later; keep LLM raw for re-extract
        for k, v in ext_fields.items():
            if k not in s.raw_fields:
                s.raw_fields[k] = v
        s.missing = list(result.missing)
        s.expires_at = self.clock() + self.cfg.session.timeout_minutes * 60
        text = followup_text(role, result.missing, optional_empty)
        if role.reply_style:
            text = f"（{role.reply_style}）\n" + text
        ask_id = self._reply(reply_to, text, thread=True)
        if ask_id:
            s.ask_message_ids.append(ask_id)
        self.store.save_session(s)
        self.store.audit(**audit, fields=result.values, result="followup")

    def _ask_confirm(
        self,
        reply_to: str,
        role: Role,
        s: Session,
        values: dict[str, Any],
        people: dict[str, str],
        tz: str,
        audit: dict,
    ) -> None:
        conv = s.raw_fields.get("__ctx__")
        raw = s.raw_fields.get("__raw__", s.text)
        # persist validated values for the confirm round-trip
        kept = {k: v for k, v in s.raw_fields.items() if k.startswith("__")}
        # store a JSON-serialisable preview of values: keep LLM-ish raw when possible
        for k, v in values.items():
            kept[k] = v
        # dates/users are bitable shapes — for re-validate on confirm we need raw-ish;
        # validate() accepts already-normalized values for most types; store as-is.
        s.raw_fields = {
            **kept,
            "__phase__": PHASE_CONFIRM,
            "__ctx__": conv,
            "__raw__": raw,
            "__people__": people,
            "__preview__": True,
        }
        for k, v in values.items():
            s.raw_fields[k] = v
        s.expires_at = self.clock() + self.cfg.session.timeout_minutes * 60
        card = confirm_card(
            role,
            values,
            people,
            tz,
            chat_id=s.chat_id,
            user_id=s.user_id,
            source_message_id=s.source_message_id,
        )
        ask_id = self._reply_card(reply_to, card, thread=True)
        if not ask_id:
            lines = preview_lines(role, values, people, tz)
            ask_id = self._reply(reply_to, "\n".join(lines), thread=True)
        if ask_id:
            s.ask_message_ids.append(ask_id)
        self.store.save_session(s)
        self.store.audit(**audit, fields=values, result="confirm")

    def _do_write(self, msg, role, s, values, people, ctx, audit, reply_to) -> None:
        try:
            if self.cfg.tables:
                wr = MultiTableWriter(self.writer, self.cfg.tables).write("requirements", values, s.source_message_id)
            else:
                wr = self.writer.write(role, values, s.source_message_id)
        except WriteError as e:
            self.store.delete_session(s)
            self.store.audit(**audit, fields=values, result=f"write_error:{e.code}")
            self._reply(reply_to, e.user_message)
            return
        self.store.delete_session(s)
        self.store.audit(**audit, fields=values, record_id=wr.record_id, result="duplicate" if wr.duplicated else "ok")
        self._reply_success(
            reply_to,
            role,
            values,
            people,
            ctx.tz,
            wr.url,
            wr.duplicated,
            thread=bool(s.followups) or s.raw_fields.get("__phase__") == PHASE_CONFIRM,
        )

    def _reply_success(self, reply_to, role, values, people, tz, url, duplicated=False, thread=False) -> None:
        card = success_card(role, values, people, tz, url, duplicated=duplicated)
        if self._reply_card(reply_to, card, thread=thread):
            return
        self._reply(reply_to, success_text(role, values, people, tz, url, duplicated=duplicated), thread=thread)

    def _reply_duplicate(self, reply_to: str, role: Role, url: str) -> None:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🔁 这条消息已记录过「{role.name}」"},
                "template": "orange",
            },
            "elements": [
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开记录"},
                            "type": "primary",
                            "url": url,
                        }
                    ],
                }
            ]
            if url
            else [{"tag": "div", "text": {"tag": "plain_text", "content": "已记录过"}}],
        }
        if self._reply_card(reply_to, card):
            return
        self._reply(reply_to, f"🔁 这条消息已记录过「{role.name}」\n{url}")

    # ------------------------------------------------------------------ misc

    def _continue_modify(self, msg: IncomingMessage, s: Session, text: str, people: dict[str, str]) -> None:
        """After card 「修改」: accept field edits or free text, then re-preview."""
        role = self.cfg.find_role(s.role)
        if role is None:
            self.store.delete_session(s)
            return
        if is_cancel(text):
            self.store.delete_session(s)
            self._reply(s.source_message_id, "已取消，未写入。", thread=True)
            return
        if not text:
            self._reply(msg.message_id, "请直接回复要改的字段，例如「优先级改成 P0」或「标题：xxx」。", thread=True)
            return
        edits = parse_edits(role, text)
        if edits:
            for k, v in edits.items():
                s.raw_fields[k] = v
            s.raw_fields.pop("__confirmed__", None)
            s.raw_fields["__phase__"] = PHASE_CONFIRM
            self._process(msg, role, s, people, skip_extract=True, force_preview=True)
            return
        # free-text: re-extract with follow-up then preview
        s.followups.append(text)
        s.raw_fields.pop("__confirmed__", None)
        s.raw_fields["__phase__"] = PHASE_CONFIRM
        self._process(msg, role, s, people, force_preview=True)

    def _react(self, message_id: str) -> None:
        try:
            self.api.add_reaction(message_id, self.cfg.react_emoji or "Get")
        except Exception as e:
            log.info("react.skipped", error=str(e)[:200])

    def handle_card_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle interactive card button (confirm / cancel / modify). Returns toast/card update dict.

        ``payload`` shape (from Feishu card.action.trigger)::
            {operator: {open_id}, action: {value: {...}}, context: {open_message_id, open_chat_id}}
        """
        action = (payload.get("action") or {}) if isinstance(payload, dict) else {}
        value = action.get("value") if isinstance(action, dict) else None
        if isinstance(value, dict) and value.get("kind") == "report_confirm":
            from .personal_agent.cards import handle_report_action

            return handle_report_action(
                self.report_store, payload,
                datetime.fromtimestamp(self.clock(), ZoneInfo(self.cfg.timezone)).date(),
            )
        if not isinstance(value, dict) or value.get("kind") != "confirm":
            return {"toast": {"type": "info", "content": "未知操作"}}
        op = value.get("action")
        chat_id = str(value.get("chat_id") or "")
        user_id = str(value.get("user_id") or "")
        role_cmd = str(value.get("role") or "")
        source = str(value.get("source") or "")
        operator = ((payload.get("operator") or {}) if isinstance(payload, dict) else {}).get("open_id") or ""
        if not operator or not user_id or operator != user_id:
            return {"toast": {"type": "error", "content": "只有发起确认的人可以操作此卡片"}}
        s = self.store.get_session(chat_id, user_id, role_cmd)
        if s is None or s.source_message_id != source:
            return {"toast": {"type": "info", "content": "确认已过期或已处理"}}
        role = self.cfg.find_role(s.role)
        if role is None:
            self.store.delete_session(s)
            return {"toast": {"type": "error", "content": "角色已失效"}}

        # Build a synthetic message so we can reuse _process / write helpers
        msg = IncomingMessage(
            event_id=f"card:{source}:{op}",
            message_id=source,
            chat_id=chat_id,
            chat_type="group",
            sender_open_id=user_id,
            message_type="text",
            content='{"text":""}',
        )
        people = s.raw_fields.get("__people__") or {}

        if op == "cancel":
            self.store.delete_session(s)
            self._reply(source, "已取消，未写入。", thread=True)
            return {"toast": {"type": "info", "content": "已取消"}}
        if op == "modify":
            s.raw_fields["__phase__"] = PHASE_MODIFY
            s.expires_at = self.clock() + self.cfg.session.timeout_minutes * 60
            self.store.save_session(s)
            self._reply(
                source,
                "请在本话题里回复要修改的内容，例如「优先级改成 P0」或「标题：新标题」。改完后我会再次请你确认。/取消 放弃。",
                thread=True,
            )
            return {"toast": {"type": "info", "content": "请在话题里回复修改"}}
        if op == "confirm":
            s.raw_fields["__confirmed__"] = True
            # Avoid treating card event as a duplicate of the original @ message
            self._process(msg, role, s, people, skip_extract=True)
            return {"toast": {"type": "success", "content": "已确认写入"}}
        return {"toast": {"type": "info", "content": f"未知动作 {op}"}}

    def expire_sessions(self) -> int:
        expired = self.store.pop_expired_sessions()
        for s in expired:
            role = self.cfg.find_role(s.role)
            name = role.name if role else "记录"
            self._reply(
                s.source_message_id,
                f"已超时取消，「{name}」本次记录未写入。可重新 @我 再提。",
                thread=True,
            )
        return len(expired)

    def _sender_name(self, msg: IncomingMessage) -> str:
        try:
            return self.api.get_user_name(msg.sender_open_id) or ""
        except Exception:
            return ""

    def _reply(self, message_id: str, text: str, thread: bool = False) -> str | None:
        try:
            return self.api.reply_text(message_id, text, in_thread=thread)
        except FeishuAPIError as e:
            log.error("reply.failed", code=e.code, msg=e.msg)
            return None

    def _reply_card(self, message_id: str, card: dict[str, Any], thread: bool = False) -> str | None:
        try:
            return self.api.reply_card(message_id, card, in_thread=thread)
        except Exception as e:
            log.info("reply.card_fallback", error=str(e)[:200])
            return None
