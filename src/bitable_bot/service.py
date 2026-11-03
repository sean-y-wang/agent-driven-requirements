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
from .requirements_v09.prd import (
    MemberDirectory,
    TaskDesk,
    dept_question,
    extract_attachments,
    parse_dept,
    parse_task_command,
    transfer_attachments,
)
from .requirements_v09.prd2 import (
    OPS_OPS,
    apply_plan,
    brief_text,
    can_view_brief,
    create_task_for_requirement,
    detect_assignee,
    dossier,
    log_status_change,
    ops_review_card,
    parse_assign_command,
    parse_schedule_command,
    plan_delay,
    plan_insert,
    plan_text,
)
from .requirements_v09.writer import MultiTableWriter
from .router import IncomingMessage, Kind, bot_mentioned, clean_text, extract_text, help_text, route
from .session import Session, Store
from .validator import Context, validate
from .writer import BitableWriter, FeishuAPI, FeishuAPIError, WriteError

log = structlog.get_logger(__name__)

PICK = "__pick__"
PHASE_CONFIRM = "confirm"
PHASE_MODIFY = "modify"  # waiting for free-text field edits after card 「修改」
PHASE_DEPT = "dept"  # first-time requester: waiting for 提出部门
PHASE_GUIDE = "guide"  # unused for session (guide does not open a session)
_HIDDEN_IN_RECEIPT = {"$context", "$raw_text"}


def _auto_guide(role: Role) -> str:
    req = "、".join(
        f.field for f in role.fields if f.required and not (isinstance(f.default, str) and f.default.startswith("$"))
    )
    style = f"（{role.reply_style}）" if role.reply_style else ""
    base = role.guide_prompt or (f"想记录到「{role.name}」的话，请尽量说清：{req or '关键内容'}。例：{role.command} ……")
    return base.strip() + (f"\n{style}" if style and role.guide_prompt is None else "")



def _soft_tip(cfg: AppConfig, chat_id: str | None = None, role: Role | None = None) -> str:
    """One short actionable line for unclear input; avoid dumping full help."""
    if cfg.dedicated and (cfg.first_guide_text or "").strip():
        return cfg.first_guide_text.strip()
    r = role or (cfg.roles[0] if cfg.dedicated and cfg.roles else None)
    if r is not None:
        tip = (r.guide_prompt or "").strip()
        if tip:
            lines = [ln.strip() for ln in tip.splitlines() if ln.strip()]
            return "\n".join(lines[:2])
        return _auto_guide(r)
    return help_text(cfg, chat_id)


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
        # passive group observe: anti-spam state (in-memory, resets on restart)
        self._obs_sender_last: dict[tuple[str, str], float] = {}
        self._obs_chat_cards: dict[str, list[float]] = {}
        self._obs_text_seen: dict[tuple[str, str], float] = {}
        # PRD batch 1: 成员配置 / 我的任务 (only with 0.9 tables)
        self.members = MemberDirectory(api, cfg.tables, store, clock=clock) if cfg.tables else None
        self.tasks = TaskDesk(api, cfg.tables, cfg.timezone, getattr(writer, "base_url", "")) if cfg.tables else None
        self._plans: dict[str, Any] = {}  # open_id -> pending SchedulePlan (中插/顺延 待确认)

    def _spec_by_type(self, role: Role | None, *, key: str | None = None, ftype: str | None = None):
        for f in (role.fields if role else []):
            if (key and f.key == key) or (ftype and f.type == ftype):
                return f
        return None

    def _attach_spec(self):
        return self._spec_by_type(self.cfg.primary_role() if self.cfg.dedicated else None, ftype="attachment")

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
                    and (msg.chat_id in self.cfg.intake.observed_chats or self.store.is_observed(msg.chat_id))
                    and (not self.cfg.chats_allowlist or msg.chat_id in self.cfg.chats_allowlist)
                    and msg.message_type in ("text", "post") and msg.sender_open_id
                    and msg.sender_open_id != self.bot_open_id):
                text, people = clean_text(raw, msg.mentions, self.bot_open_id, strip_leading=False)
                sender_name = ""
                try:
                    sender_name = self.collector.chat_members(msg.chat_id).get(msg.sender_open_id, "")
                except Exception:
                    pass
                log.info("observe.event", chat_id=msg.chat_id, message_id=msg.message_id,
                         sender_open_id=msg.sender_open_id, sender_name=sender_name, text_len=len(text or ""))
                try:
                    self._detect(msg, text, people, passive=True)
                except Exception:
                    log.exception("observe.failed", message_id=msg.message_id)
            return
        if msg.message_type in ("image", "file", "media") and self.cfg.tables and self._attach_spec() is not None:
            self._attach_to_session(msg, thread_s or (sessions[0] if sessions else None))
            return
        if msg.message_type not in ("text", "post"):
            self._reply(msg.message_id, "暂只支持文字消息。发送 /help 查看用法。")
            return
        if self.cfg.chats_allowlist and not p2p and msg.chat_id not in self.cfg.chats_allowlist:
            self._reply(msg.message_id, "抱歉，本群未开通记录功能，请联系管理员。")
            return
        text, people = clean_text(raw, msg.mentions, self.bot_open_id, strip_leading=not p2p)
        if (not p2p and thread_s is None and self.cfg.tables and self.cfg.intake.observe_groups
                and (text or "").strip(" 。!！") in ("开启监听", "关闭监听", "监听状态")):
            self._observe_command(msg, (text or "").strip(" 。!！"))
            return
        if p2p and self.tasks is not None and thread_s is None and not (
            sessions and sessions[0].raw_fields.get("__phase__") == PHASE_DEPT
        ):
            tcmd = parse_task_command(text, self.tasks.statuses())
            if tcmd is not None:
                self._task_command(msg, tcmd)
                return
            today = datetime.fromtimestamp(self.clock(), ZoneInfo(self.cfg.timezone)).date()
            scmd = parse_schedule_command(text, today)
            if scmd is not None:
                self._schedule_command(msg, scmd)
                return
            acmd = parse_assign_command(text)
            if acmd is not None and self.cfg.ops_review.enabled:
                self._assign_command(msg, acmd, people)
                return
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
            self._reply(msg.message_id, f"没认出命令 {r.command}。直接说需求内容，或发 /help。")
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
            elif s.raw_fields.get("__phase__") == PHASE_DEPT:
                self._continue_dept(msg, s, text, people)
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
            self._reply(msg.message_id, _soft_tip(self.cfg, msg.chat_id))
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
                self._reply(msg.message_id, _soft_tip(self.cfg, msg.chat_id))
            return
        provider = self.extractor.resolve(self.cfg.intent.llm)
        intent = classify(provider, self.cfg, roles, text, conv.excerpt)
        if self.cfg.tables:
            # No optimistic record fallback when classification fails in 0.9.
            if intent.confidence < self.cfg.intent.min_confidence or not intent.action:
                if not passive:
                    self._reply(msg.message_id, _soft_tip(self.cfg, msg.chat_id, dedicated_role or (roles[0] if roles else None)))
                return
            role = dedicated_role or intent.role
            if passive:
                log.info("observe.intent", message_id=msg.message_id, action=intent.action, confidence=intent.confidence)
                if role and intent.action in ("record", "candidate") and self._observe_allow(msg, text):
                    # Suspected requirement: confirm card to the speaker; nothing is written until confirmed.
                    self._start(msg, role, text, people, raw_text=text, conv=conv, intake="passive")
                # progress / other: gray stage does not auto-write anything
                return
            if role and intent.action == "progress":
                if msg.chat_type == "group":
                    self._group_progress(msg, text, role)
                elif not passive:
                    self._reply(msg.message_id, "私聊可直接提需求或问进展。要记今天进展，可以说「记一下今天进展：……」。")
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
            self._reply(msg.message_id, _soft_tip(self.cfg, msg.chat_id, dedicated_role))
            return

        if intent.role is not None and intent.confidence >= self.cfg.intent.min_confidence:
            self._start(msg, intent.role, text, people, raw_text=text, conv=conv)
            return
        # Dedicated bot never opens a role-pick session; unclear → guide.
        if dedicated_role is not None:
            self._reply(msg.message_id, _soft_tip(self.cfg, msg.chat_id, dedicated_role))
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
        if any(getattr(f, "key", None) == "tag" for f in (getattr(self.cfg.tables.progress, "fields", None) or [])):
            values["tag"] = "风险" if values["risk"] else "进展"
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
    def _observe_command(self, msg: IncomingMessage, cmd: str) -> None:
        if msg.sender_open_id not in self.cfg.intake.observe_admins:
            log.info("observe.cmd_denied", chat_id=msg.chat_id, sender_open_id=msg.sender_open_id, cmd=cmd)
            self._reply(msg.message_id, "没有权限")
            return
        if cmd == "开启监听":
            self.store.set_observe(msg.chat_id, True, msg.sender_open_id)
            reply = "已开启本群监听：识别到疑似需求时会发确认卡，提出人确认后才入池。"
        elif cmd == "关闭监听":
            self.store.set_observe(msg.chat_id, False, msg.sender_open_id)
            reply = "已关闭本群监听。"
        else:
            on = msg.chat_id in self.cfg.intake.observed_chats or self.store.is_observed(msg.chat_id)
            reply = "本群监听：已开启" if on else "本群监听：未开启"
        log.info("observe.cmd", chat_id=msg.chat_id, sender_open_id=msg.sender_open_id, cmd=cmd)
        self._reply(msg.message_id, reply)

    def _observe_allow(self, msg: IncomingMessage, text: str) -> bool:
        """Dedup + rate limit for passive confirm cards."""
        now = self.clock()
        key = " ".join((text or "").split())[:200]
        if self._obs_text_seen.get((msg.chat_id, key), 0) > now - 86400:
            log.info("observe.skip", reason="dup_text", message_id=msg.message_id)
            return False
        if self._obs_sender_last.get((msg.chat_id, msg.sender_open_id), 0) > now - 600:
            log.info("observe.skip", reason="sender_cooldown", message_id=msg.message_id)
            return False
        recent = [t for t in self._obs_chat_cards.get(msg.chat_id, []) if t > now - 3600]
        if len(recent) >= 5:
            log.info("observe.skip", reason="chat_rate", message_id=msg.message_id)
            return False
        if self.store.sessions_for(msg.chat_id, msg.sender_open_id):
            log.info("observe.skip", reason="pending_session", message_id=msg.message_id)
            return False
        self._obs_text_seen[(msg.chat_id, key)] = now
        self._obs_sender_last[(msg.chat_id, msg.sender_open_id)] = now
        self._obs_chat_cards[msg.chat_id] = recent + [now]
        return True

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
        quiet = intake in ("candidate", "passive")
        if not self._permitted(msg, role):
            if intake == "passive":
                return
            self._reply(source_id, f"抱歉，你在这里没有使用「{role.name}」的权限。")
            return
        lookup_key = MultiTableWriter(self.writer, self.cfg.tables).key("requirements", source_id) if self.cfg.tables else source_id
        existing = self.store.get_record(lookup_key)
        if existing:
            if not quiet:
                self._reply_duplicate(source_id, role, existing[1])
            return
        if conv is None:
            conv = self.collector.collect(msg)
        if intake == "passive" and role.min_content_chars > 0 and _is_thin(body, conv.excerpt, role.min_content_chars):
            return
        if not quiet and role.min_content_chars > 0 and _is_thin(body, conv.excerpt, role.min_content_chars):
            tip = _soft_tip(self.cfg, msg.chat_id, role) if self.cfg.dedicated else _auto_guide(role)
            self._reply(source_id, tip)
            return
        if not body and not conv.excerpt:
            if intake == "passive":
                return
            req = "、".join(f.field for f in role.fields if f.required)
            self._reply(source_id, f"请在命令后附上内容，例如：{role.command} ……（需要包含：{req}）")
            return
        for old in self.store.sessions_for(msg.chat_id, msg.sender_open_id):
            if not quiet and old.role in (role.command, PICK):
                self.store.delete_session(old)
        s = Session(
            chat_id=msg.chat_id,
            user_id=msg.sender_open_id,
            role=role.command,
            source_message_id=source_id,
            text=body,
            raw_fields={"__ctx__": conv.to_dict(), "__people__": people, "__raw__": raw_text, "__intake__": intake, "__chat_type__": msg.chat_type},
        )
        if self.cfg.tables and self._attach_spec() is not None:
            items = extract_attachments(msg.message_type, msg.content, msg.message_id)
            if items:
                s.raw_fields["__attachments__"] = items
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
            self._reply(s.source_message_id, "已取消，没有收录。", thread=True)
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
        passive = s.raw_fields.get("__intake__") == "passive"
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
            if self.cfg.ack_after_seconds > 0 and not candidate and not passive:

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
                if candidate or passive:
                    return
                self._reply(reply_to, "刚才没整理好，稍后在这再回一句，或 /取消。", thread=True)
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
            ext_fields.update(status="待分诊" if (candidate or passive) else "待评审", review=bool(candidate or passive),
                              agent="群" if s.raw_fields.get("__chat_type__", msg.chat_type) == "group" else "其他")
        dept_spec = self._spec_by_type(role, key="dept") if self.cfg.tables else None
        dept = s.raw_fields.get("__dept__")
        if dept_spec is not None and dept is None and self.members is not None:
            dept = self.members.dept_of(s.user_id)
        if dept_spec is not None and dept:
            ext_fields["dept"] = dept
        if self.cfg.tables and self._spec_by_type(role, key="assignee") is not None and not ext_fields.get("assignee"):
            picked = detect_assignee(raw_text, all_people, exclude=(self.bot_open_id or "", s.user_id))
            if picked:
                ext_fields["assignee"] = [{"id": o} for o in picked]
                log.info("assignee.detected", n=len(picked))
            elif self.members is not None and not (candidate or passive):
                ops = self._ops_ids()
                if ops:  # PRD：没指定就填产品运营
                    ext_fields["assignee"] = [{"id": ops[0]}]
        result = validate(role.fields, ext_fields, ctx)
        if passive and not s.raw_fields.get("__confirmed__") and not s.raw_fields.get("__preview__"):
            if not result.complete:
                self.store.audit(**{**audit, "text": None}, fields=result.values, result="observe:incomplete")
                return
            self._ask_confirm(reply_to, role, s, result.values, all_people, ctx.tz, audit)
            log.info("observe.confirm_card", message_id=s.source_message_id, sender_open_id=s.user_id)
            return
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
            if (need_confirm or force_preview) and dept_spec is not None and dept is None and not passive:
                self._ask_dept(reply_to, role, s, result.values, all_people, dept_spec, audit)
                return
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
            self.store.audit(**{**audit, "text": None}, fields=result.values, result="guide:vague_extract")
            tip = _soft_tip(self.cfg, msg.chat_id, role)
            self._reply(reply_to, tip)
            return

        if len(s.followups) >= self.cfg.session.max_followups:
            self.store.delete_session(s)
            self.store.audit(**audit, fields=result.values, result="abandoned:max_followups")
            names = "、".join(f.field for f in role.fields if f.key in result.missing)
            self._reply(
                reply_to,
                f"还缺：{names}。这次先不算了，补全后直接再说一遍需求就行。",
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
        if s.raw_fields.get("__intake__") == "passive" and isinstance(card, dict) and isinstance(card.get("elements"), list):
            card["elements"].insert(0, {"tag": "div", "text": {"tag": "lark_md",
                "content": f"<at id={s.user_id}></at> 这条像是一条需求，确认要收录吗？（仅提出人可确认）"}})
        ask_id = self._reply_card(reply_to, card, thread=True)
        if not ask_id:
            lines = preview_lines(role, values, people, tz)
            ask_id = self._reply(reply_to, "\n".join(lines), thread=True)
        if ask_id:
            s.ask_message_ids.append(ask_id)
        self.store.save_session(s)
        self.store.audit(**audit, fields=values, result="confirm")

    def _do_write(self, msg, role, s, values, people, ctx, audit, reply_to) -> None:
        att_spec = self._spec_by_type(role, ftype="attachment") if self.cfg.tables else None
        items = s.raw_fields.get("__attachments__") or []
        if att_spec is not None and items:
            tokens = transfer_attachments(self.api, self.cfg.tables.requirements.app_token, items)
            log.info("attachment.transferred", ok=len(tokens), total=len(items))
            if tokens:
                values = {**values, att_spec.key: tokens}
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
        if self.cfg.tables and not wr.duplicated:
            self._after_intake(role, s, values, wr, reply_to)
        card = success_card(role, values, people, ctx.tz, wr.url, duplicated=wr.duplicated)
        # Card-action confirm path: return receipt via callback (toast+card) instead of a
        # second reply_card, so we ACK within Feishu/xfchat's ~3s window (else client
        # shows code 300000 even though the row was already written).
        if getattr(self, "_card_action_receipt", False):
            self._last_card_receipt = card
            return
        thread = bool(s.followups) or s.raw_fields.get("__phase__") == PHASE_CONFIRM
        if self._reply_card(reply_to, card, thread=thread):
            return
        self._reply(
            reply_to,
            success_text(role, values, people, ctx.tz, wr.url, duplicated=wr.duplicated),
            thread=thread,
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
                "title": {"tag": "plain_text", "content": "🔁 这条已经记过了"},
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
        self._reply(reply_to, f"🔁 这条已经记过了\n{url}")

    # ------------------------------------------------------------------ PRD batch 1
    def _ask_dept(self, reply_to, role, s, values, people, spec, audit) -> None:
        kept = {k: v for k, v in s.raw_fields.items() if k.startswith("__")}
        s.raw_fields = {**kept, **values, "__phase__": PHASE_DEPT, "__people__": people}
        s.expires_at = self.clock() + self.cfg.session.timeout_minutes * 60
        ask_id = self._reply(reply_to, dept_question(spec), thread=True)
        if ask_id:
            s.ask_message_ids.append(ask_id)
        self.store.save_session(s)
        self.store.audit(**{**audit, "text": None}, fields=values, result="ask_dept")

    def _continue_dept(self, msg: IncomingMessage, s: Session, text: str, people: dict[str, str]) -> None:
        role = self.cfg.find_role(s.role)
        spec = self._spec_by_type(role, key="dept")
        if role is None or spec is None:
            self.store.delete_session(s)
            return
        rid = s.raw_fields.get("__fill_record__")
        if rid:  # 群里被动收录后补问部门：直接回填那条记录
            if is_cancel(text) or is_skip(text):
                dept = []
            else:
                dept = parse_dept(text, spec) or []
                if not dept:
                    self._reply(msg.message_id, "没认出部门，请回复序号（如「2」）或部门名；不想填回「跳过」。", thread=True)
                    return
            self.store.delete_session(s)
            if self.members is not None:
                self.members.remember_dept(s.user_id, (s.raw_fields.get("__ctx__") or {}).get("sender_name") or "", dept)
            if dept:
                try:
                    t = self.cfg.tables.requirements
                    self.api.update_record(t.app_token, t.table_id, rid, {spec.field: dept})
                    self._reply(msg.message_id, f"✅ 已补上{spec.field}：{'、'.join(dept)}，以后自动沿用。", thread=True)
                except Exception as e:
                    log.info("dept.fill_failed", error=str(e)[:200])
                    self._reply(msg.message_id, "部门记下了，但回填表格失败，请在表里手动补一下。", thread=True)
            else:
                self._reply(msg.message_id, "好的，不填部门，以后也不再问。", thread=True)
            return
        if is_cancel(text):
            self.store.delete_session(s)
            self._reply(s.source_message_id, "已取消，没有收录。", thread=True)
            return
        if is_skip(text):
            dept: list[str] = []
        else:
            dept = parse_dept(text, spec) or []
            if not dept:
                self._reply(msg.message_id, "没认出部门，请回复序号（如「2」）或部门名；不想填回「跳过」。", thread=True)
                return
        s.raw_fields["__dept__"] = dept
        if dept:
            s.raw_fields["dept"] = dept
        s.raw_fields["__phase__"] = PHASE_CONFIRM
        if self.members is not None:
            name = (s.raw_fields.get("__ctx__") or {}).get("sender_name") or ""
            self.members.remember_dept(s.user_id, name, dept)
        self._process(msg, role, s, people, skip_extract=True, force_preview=True)

    # ------------------------------------------------------------------ PRD batch 2
    def _ops_ids(self) -> list[str]:
        if self.cfg.ops_review.open_ids:
            return list(self.cfg.ops_review.open_ids)
        try:
            return self.members.by_role("产品运营") if self.members is not None else []
        except Exception:
            return []

    def _after_intake(self, role: Role, s: Session, values: dict, wr, reply_to: str) -> None:
        """入池后：群被动收录补问部门；产品运营评估卡（默认关闭）。都不影响入池结果。"""
        try:
            spec = self._spec_by_type(role, key="dept")
            if (s.raw_fields.get("__intake__") == "passive" and spec is not None and not values.get("dept")
                    and self.members is not None and self.members.dept_of(s.user_id) is None):
                ns = Session(chat_id=s.chat_id, user_id=s.user_id, role=s.role, source_message_id=s.source_message_id,
                             text="", raw_fields={"__phase__": PHASE_DEPT, "__fill_record__": wr.record_id,
                                                  "__ctx__": s.raw_fields.get("__ctx__")},
                             expires_at=self.clock() + self.cfg.session.timeout_minutes * 60)
                ask_id = self._reply(reply_to, f"<at user_id=\"{s.user_id}\"></at> 已收录。补一下你的部门吧（在本话题回复）：\n"
                                     + dept_question(spec), thread=True)
                if ask_id:
                    ns.ask_message_ids.append(ask_id)
                self.store.save_session(ns)
        except Exception:
            log.exception("intake.passive_dept_failed")
        if self.cfg.ops_review.enabled:
            try:
                ops = self._ops_ids()
                to = list(dict.fromkeys(ops + [p.get("id") for p in (values.get("assignee") or []) if isinstance(p, dict)]))
                name = (s.raw_fields.get("__ctx__") or {}).get("sender_name") or ""
                card = ops_review_card(str(values.get("title") or ""), str(values.get("desc") or ""), name,
                                       "、".join(values.get("dept") or []), wr.record_id, wr.url or "")
                for oid in to:
                    if oid:
                        self.api.send_card(oid, card, receive_id_type="open_id")
                log.info("ops_review.sent", n=len(to), record_id=wr.record_id)
            except Exception:
                log.exception("ops_review.failed")

    def _after_report_confirm(self, report_id: str) -> None:
        """日报确认 → 风险同步产品运营（reports.risk_to_ops）；周报确认 → 写当周云文档（weekly_doc.enabled）。"""
        try:
            d = self.report_store.get(report_id) if self.report_store is not None else None
            if not d or d.get("status") != "confirmed":
                return
            body = d.get("body") or ""
            if d["kind"] == "daily" and self.cfg.reports.risk_to_ops and "风险预警" in body:
                name = self.api.get_user_name(d["owner"]) if hasattr(self.api, "get_user_name") else d["owner"]
                risk = body[body.index("风险预警"):]
                for oid in self._ops_ids():
                    if oid != d["owner"]:
                        self.api.send_text(oid, f"{name} 的日报风险：\n{risk}", receive_id_type="open_id")
            if d["kind"] == "weekly" and self.cfg.weekly_doc.enabled and getattr(self, "doc_http", None) is not None:
                from datetime import date as _date

                from .requirements_v09.prd2 import WeeklyDoc

                w = self.cfg.weekly_doc
                doc = WeeklyDoc(self.doc_http, folder_token=w.folder_token, wiki_space_id=w.wiki_space_id,
                                wiki_parent_node=w.wiki_parent_node, title_fmt=w.title_fmt,
                                base_url=getattr(self.writer, "base_url", ""))
                name = self.api.get_user_name(d["owner"]) if hasattr(self.api, "get_user_name") else d["owner"]
                url = doc.append_section(_date.fromisoformat(d["day"]), name or d["owner"], body)
                log.info("weekly_doc.appended", owner=d["owner"], url=url)
        except Exception:
            log.exception("report.after_confirm_failed")

    def _schedule_command(self, msg: IncomingMessage, cmd) -> None:
        uid = msg.sender_open_id
        tables = self.cfg.tables
        try:
            if cmd.kind == "brief":
                rows = self.api.list_all_records(tables.tasks.app_token, tables.tasks.table_id)
                mine = self.tasks.mine(uid)
                title = {f.key: f.field for f in tables.tasks.fields}["title"]
                if cmd.target.isdigit() and 1 <= int(cmd.target) <= len(mine):
                    hits = [mine[int(cmd.target) - 1]]
                else:
                    hits = [r for r in rows if cmd.target in str((r.get("fields") or {}).get(title) or "")]
                if len(hits) != 1:
                    text = "没唯一匹配到任务，换个更完整的任务名，或先发「我的任务」看序号。" if not hits else \
                        "匹配到多个任务，请写全名：" + "；".join(str(r["fields"].get(title)) for r in hits[:5])
                else:
                    d = dossier(self.api, tables, hits[0]["record_id"], self.cfg.timezone, tasks=rows,
                                base_url=getattr(self.writer, "base_url", ""))
                    priv = set(self._ops_ids()) | set(self.members.by_role("管理者") if self.members else [])
                    text = brief_text(d) if can_view_brief(d, uid, priv) else "这个任务的简报只回给参与人、需求提出人和产品运营。"
            elif cmd.kind == "confirm":
                plan = self._plans.pop(uid, None)
                if plan is None or self.clock() - plan.created > 600:
                    text = "没有待确认的排期变更（或已超过 10 分钟），请重新发「中插 …」或「顺延 …」。"
                else:
                    text = apply_plan(self, plan, uid)
            else:
                mine = self.tasks.mine(uid)
                if cmd.kind == "insert":
                    from datetime import date as _date
                    plan, err = plan_insert(mine, tables, cmd.target, _date.fromisoformat(cmd.date), cmd.days, self.cfg.timezone)
                else:
                    plan, err = plan_delay(mine, tables, cmd.target, cmd.days, self.cfg.timezone)
                if plan is None:
                    text = err
                else:
                    plan.owner, plan.created = uid, self.clock()
                    self._plans[uid] = plan
                    text = plan_text(plan)
            result = f"schedule:{cmd.kind}"
        except Exception as e:
            log.exception("schedule.command_failed")
            text, result = f"操作失败，请稍后再试（{str(e)[:80]}）。", f"schedule:{cmd.kind}:error"
        self.store.audit(chat_id=msg.chat_id, sender=uid, command="schedule", result=result,
                         fields={"target": cmd.target, "date": cmd.date, "days": cmd.days})
        self._reply(msg.message_id, text)

    def _assign_command(self, msg: IncomingMessage, cmd, people: dict[str, str]) -> None:
        rid, who = cmd
        if msg.sender_open_id not in self._ops_ids():
            self._reply(msg.message_id, "只有产品运营可以分配执行人。")
            return
        oid = people.get(who.lstrip("@")) if people else None
        if not oid and people and len(people) == 1:
            oid = next(iter(people.values()))
        if not oid and who.startswith("ou_"):
            oid = who
        if not oid:
            self._reply(msg.message_id, "请 @ 要分配的人，例如「分配 recXXX 给 @张三」。")
            return
        try:
            tid = create_task_for_requirement(self.api, self.writer, self.cfg.tables, rid, oid)
            d = dossier(self.api, self.cfg.tables, tid, self.cfg.timezone, base_url=getattr(self.writer, "base_url", ""))
            if not self.cfg.schedule_notify.dry_run:
                self.api.send_text(oid, "你被分配了一个任务 👇\n" + brief_text(d), receive_id_type="open_id")
            self._reply(msg.message_id, f"✅ 已建研发任务并分配；{'已' if not self.cfg.schedule_notify.dry_run else '（试运行）未'}给执行人发简报。")
        except Exception as e:
            log.exception("assign.failed")
            self._reply(msg.message_id, f"分配失败：{str(e)[:80]}")

    def handle_ops_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = ((payload.get("action") or {}).get("value") or {})
        operator = (payload.get("operator") or {}).get("open_id") or ""
        if operator not in self._ops_ids():
            return {"toast": {"type": "error", "content": "只有产品运营可以评估"}}
        op = OPS_OPS.get(value.get("op") or "")
        rid = value.get("record_id") or ""
        if not op or not rid.startswith("rec"):
            return {}
        rc = {f.key: f.field for f in self.cfg.tables.requirements.fields}
        fields = {rc[k]: v for k, v in op.items() if k in rc}
        t = self.cfg.tables.requirements
        threading.Thread(target=lambda: self.api.update_record(t.app_token, t.table_id, rid, fields), daemon=True).start()
        return {"toast": {"type": "success", "content": f"已改为 {op.get('status')}"}}

    def _attach_to_session(self, msg: IncomingMessage, s: Session | None) -> None:
        items = extract_attachments(msg.message_type, msg.content, msg.message_id)
        thread = msg.chat_type != "p2p"
        if not items:
            return
        if s is None or s.role == PICK:
            if msg.chat_type == "p2p":
                self._reply(msg.message_id, "收到附件。先跟我说需求内容，确认收录前再发图片或文件，我会一起存进「附件」。")
            return
        s.raw_fields.setdefault("__attachments__", []).extend(items)
        self.store.save_session(s)
        n = len(s.raw_fields["__attachments__"])
        self._reply(msg.message_id, f"📎 已收到附件（共 {n} 个），收录时会一并存入「附件」。", thread=thread)

    def _task_command(self, msg: IncomingMessage, cmd) -> None:
        try:
            if cmd.kind == "list":
                text = self.tasks.list_text(msg.sender_open_id)
            else:
                text = self.tasks.set_status(msg.sender_open_id, cmd.target, cmd.status)
                if self.tasks.last_change is not None:
                    row, old, new = self.tasks.last_change
                    if log_status_change(self.writer, self.cfg.tables, row, old, new, msg.sender_open_id,
                                         self.cfg.timezone, datetime.fromtimestamp(self.clock(), ZoneInfo(self.cfg.timezone))):
                        text += "\n已在进展日志记一笔。"
            result = f"task:{cmd.kind}"
        except Exception as e:
            log.exception("task.command_failed")
            text, result = f"读取任务表失败，请稍后再试（{str(e)[:80]}）。", f"task:{cmd.kind}:error"
        self.store.audit(chat_id=msg.chat_id, sender=msg.sender_open_id, command="task", result=result,
                         fields={"target": cmd.target, "status": cmd.status})
        self._reply(msg.message_id, text)

    # ------------------------------------------------------------------ misc

    def _continue_modify(self, msg: IncomingMessage, s: Session, text: str, people: dict[str, str]) -> None:
        """After card 「修改」: accept field edits or free text, then re-preview."""
        role = self.cfg.find_role(s.role)
        if role is None:
            self.store.delete_session(s)
            return
        if is_cancel(text):
            self.store.delete_session(s)
            self._reply(s.source_message_id, "已取消，没有收录。", thread=True)
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
            log.info("react.skipped", emoji=self.cfg.react_emoji, error=str(e)[:200])
            if (self.cfg.react_emoji or "Get") != "Get":
                try:
                    self.api.add_reaction(message_id, "Get")
                except Exception as e2:
                    log.info("react.fallback_skipped", error=str(e2)[:200])

    BOT_ADDED_TEXT = (
        "你好，我是需求小助手。\n"
        "· 提需求：@我 说需求标题和背景，确认后入池；也可私聊我。\n"
        "· 查进展：@我 问「XX需求进展」；私聊发「我的任务」可查看并修改分给自己的任务状态。\n"
        "· 本群默认不监听未@的消息；白名单成员 @我「开启监听」后会识别疑似需求并请提出人确认，@我「关闭监听」即停止，「监听状态」可查看。"
    )

    def handle_bot_added(self, chat_id: str) -> None:
        """Post a short usage note when the bot joins a group (best-effort)."""
        if not chat_id:
            return
        try:
            self.api.send_text(chat_id, self.BOT_ADDED_TEXT, receive_id_type="chat_id")
            log.info("bot_added.intro_sent", chat_id=chat_id)
        except Exception as e:
            log.info("bot_added.intro_skipped", error=str(e)[:200])

    def handle_card_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle interactive card button (confirm / cancel / modify). Returns toast/card update dict.

        ``payload`` shape (from Feishu card.action.trigger)::
            {operator: {open_id}, action: {value: {...}}, context: {open_message_id, open_chat_id}}
        """
        action = (payload.get("action") or {}) if isinstance(payload, dict) else {}
        value = action.get("value") if isinstance(action, dict) else None
        if isinstance(value, dict) and value.get("kind") == "report_confirm":
            from .personal_agent.cards import handle_report_action

            out = handle_report_action(
                self.report_store, payload,
                datetime.fromtimestamp(self.clock(), ZoneInfo(self.cfg.timezone)).date(),
            )
            if out and (value.get("action") or "confirm") == "confirm" and self.cfg.tables is not None:
                threading.Thread(target=self._after_report_confirm, args=(str(value.get("report_id") or ""),),
                                 daemon=True).start()
            return out
        if isinstance(value, dict) and value.get("kind") == "ops_review" and self.cfg.tables is not None:
            return self.handle_ops_action(payload)
        if not isinstance(value, dict) or value.get("kind") != "confirm":
            return {}  # hide unknown card noise
        op = value.get("action")
        chat_id = str(value.get("chat_id") or "")
        user_id = str(value.get("user_id") or "")
        role_cmd = str(value.get("role") or "")
        source = str(value.get("source") or "")
        operator = ((payload.get("operator") or {}) if isinstance(payload, dict) else {}).get("open_id") or ""
        if not operator or not user_id or operator != user_id:
            ps = self.store.get_session(chat_id, user_id, role_cmd) if user_id else None
            if ps is not None and ps.raw_fields.get("__intake__") == "passive":
                return {"toast": {"type": "error", "content": "仅提出人可确认"}}
            return {"toast": {"type": "error", "content": "只有本人可以操作这张卡片"}}
        s = self.store.get_session(chat_id, user_id, role_cmd)
        if s is None or s.source_message_id != source:
            return {}  # hide expired/processed noise
        role = self.cfg.find_role(s.role)
        if role is None:
            self.store.delete_session(s)
            return {}

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
            cancelled = {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "已取消"}, "template": "grey"},
                "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": "没有收录。"}}],
            }
            return {
                "toast": {"type": "info", "content": "已取消", "i18n": {"zh_cn": "已取消"}},
                "card": {"type": "raw", "data": cancelled},
            }
        if op == "modify":
            s.raw_fields["__phase__"] = PHASE_MODIFY
            s.expires_at = self.clock() + self.cfg.session.timeout_minutes * 60
            self.store.save_session(s)
            modify_card = {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "请修改"}, "template": "blue"},
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                "直接回复要改的内容，例如「优先级改成 P0」。"
                                "改完我会再请你确认；不想继续回 /取消。"
                            ),
                        },
                    }
                ],
            }
            return {
                "toast": {
                    "type": "info",
                    "content": "请在话题里回复修改",
                    "i18n": {"zh_cn": "请在话题里回复修改"},
                },
                "card": {"type": "raw", "data": modify_card},
            }
        if op == "confirm":
            s.raw_fields["__confirmed__"] = True
            # Avoid treating card event as a duplicate of the original @ message
            self._card_action_receipt = True
            self._last_card_receipt = None
            t0 = time.monotonic()
            try:
                self._process(msg, role, s, people, skip_extract=True)
            finally:
                self._card_action_receipt = False
            receipt = self._last_card_receipt
            self._last_card_receipt = None
            ms = int((time.monotonic() - t0) * 1000)
            log.info("card.confirm_done", ms=ms, has_receipt=isinstance(receipt, dict))
            out: dict[str, Any] = {
                "toast": {"type": "success", "content": "已收录", "i18n": {"zh_cn": "已收录"}},
            }
            if isinstance(receipt, dict):
                out["card"] = {"type": "raw", "data": receipt}
            return out
        return {}

    def expire_sessions(self) -> int:
        expired = self.store.pop_expired_sessions()
        for s in expired:
            role = self.cfg.find_role(s.role)
            name = role.name if role else "记录"
            self._reply(
                s.source_message_id,
                f"这条确认超时了，没有收录。重新跟我说一遍就行。",
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
