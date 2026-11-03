"""Progress-query orchestration: keywords → Bitable search → LLM rerank → reply text/card."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from ..config.models import AppConfig, Role
from ..providers.base import LLMProvider
from ..providers.openai_compat import provider_chain
from ..writer.feishu_api import FeishuAPI
from .keywords import extract_keywords, is_own_progress_query
from .search import search_candidates
from .summarize import rerank_and_summarize

log = structlog.get_logger(__name__)


@dataclass
class QueryResult:
    text: str
    summary: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def record_ids(self) -> list[str]:
        return [str(r.get("record_id") or "") for r in self.rows if r.get("record_id")]


class QueryService:
    def __init__(
        self,
        api: FeishuAPI,
        cfg: AppConfig,
        providers: dict[str, LLMProvider],
        default_provider: str,
        base_url: str = "https://feishu.cn",
    ) -> None:
        self.api = api
        self.cfg = cfg
        self.providers = providers
        self.default = default_provider
        self.base_url = base_url

    def run(
        self,
        role: Role,
        query_text: str,
        keywords: list[str] | None = None,
        *,
        sender_name: str = "",
    ) -> QueryResult:
        """Search + summarize. Never writes a session or a record."""
        if not role.queryable:
            msg = f"「{role.name}」暂不支持查询进展，请换一个可查询的角色，或直接登记新内容。"
            return QueryResult(text=msg, summary=msg)
        prefer_own = is_own_progress_query(query_text)
        kws = extract_keywords(query_text, keywords)
        log.info(
            "query.start",
            role=role.command,
            keywords=kws,
            prefer_own=prefer_own,
            sender=sender_name[:40] if sender_name else "",
        )
        candidates, source = search_candidates(
            self.api,
            role,
            kws,
            proposer_name=sender_name,
            prefer_own=prefer_own,
        )
        if not candidates:
            label = query_text.strip() or " ".join(kws) or "相关"
            hint = (self.cfg.query_docs_hint or "").strip()
            lines = [f"没找到和「{label}」相关的需求。"]
            if hint:
                lines.append(hint)
            else:
                lines.append("可以换个关键词再问，或直接说新需求让我帮你记。")
            text = "\n".join(lines)
            return QueryResult(text=text, summary=text)
        provider = provider_chain(self.providers, role.llm or self.default, self.cfg.llm.fallback)
        summary, reply, chosen = rerank_and_summarize(
            provider,
            role,
            query_text or " ".join(kws),
            candidates,
            base_url=self.base_url,
            prefer_own=prefer_own,
            sender_name=sender_name,
        )
        log.info("query.done", role=role.command, source=source, hits=len(chosen))
        return QueryResult(text=reply, summary=summary, rows=chosen)
