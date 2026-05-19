from __future__ import annotations

import asyncio
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from portfolio_agent.deps import Deps
from portfolio_agent.prompts import read_text, render
from portfolio_agent.rules import PreprocessCfg, load_rules
from portfolio_agent.secrets_scrub import scrub
from portfolio_agent.state import PortfolioState
from portfolio_agent.types import ChatRecord, PreprocessedChat

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# LLM output schema
# ---------------------------------------------------------------------------

class PreprocessDecision(BaseModel):
    message_index: int = Field(ge=0)
    action: Literal["KEEP", "DROP"]
    reason: str = Field(max_length=120)


class PreprocessOutput(BaseModel):
    decisions: list[PreprocessDecision]


# ---------------------------------------------------------------------------
# Stage 1 — deterministic filter (pure Python, no LLM)
# ---------------------------------------------------------------------------

def deterministic_filter(
    messages: list[dict],
    cfg: PreprocessCfg,
) -> tuple[list[dict], list[str]]:
    kept: list[dict] = []
    reasons: list[str] = []
    drops_lower = {p.lower() for p in cfg.drop_phrases_exact}

    for i, m in enumerate(messages):
        content = (m.get("content") or "").strip()
        has_code = "```" in content

        if not content:
            reasons.append(f"msg[{i}]:empty")
            continue
        if content.lower() in drops_lower:
            reasons.append(f"msg[{i}]:filler_phrase")
            continue
        if len(content) < cfg.drop_if_shorter_than_chars and not (
            cfg.keep_if_contains_code_block and has_code
        ):
            reasons.append(f"msg[{i}]:too_short")
            continue
        kept.append(m)

    return kept, reasons


# ---------------------------------------------------------------------------
# Stage 2 — LLM filter
# ---------------------------------------------------------------------------

async def _preprocess_one(
    chat: ChatRecord,
    cfg: PreprocessCfg,
    deps: Deps,
) -> PreprocessedChat | None:
    # 0. Scrub secrets before anything touches an LLM.
    scrubbed = [
        {**m, "content": scrub(m.get("content", ""))} for m in chat["messages"]
    ]

    # 1. Deterministic pass.
    msgs, reasons = deterministic_filter(scrubbed, cfg)
    if len(msgs) < cfg.min_messages_after_filter:
        return None

    # 2. LLM pass.
    out: PreprocessOutput = await deps.llm_preprocess.invoke_structured(
        system=read_text("preprocess_system.txt"),
        user=render("preprocess_user.j2", messages=list(enumerate(msgs))),
        schema=PreprocessOutput,
    )
    drop_idx = {d.message_index for d in out.decisions if d.action == "DROP"}
    for d in out.decisions:
        if d.action == "DROP":
            reasons.append(f"llm_msg[{d.message_index}]:{d.reason}")

    final = [m for i, m in enumerate(msgs) if i not in drop_idx]

    if len(final) < cfg.min_messages_after_filter:
        return None
    if sum(len(m.get("content", "")) for m in final) < cfg.min_chat_length_chars:
        return None

    return PreprocessedChat(
        chat_id=chat["chat_id"],
        user_id=chat["user_id"],
        user_email=chat["user_email"],
        started_at=chat["started_at"],
        ended_at=chat["ended_at"],
        project_id=chat.get("project_id"),
        original_message_count=len(chat["messages"]),
        filtered_message_count=len(final),
        messages=final,
        removal_reasons=reasons,
        metadata=chat.get("metadata", {}),
    )


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def preprocess_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    cfg = load_rules(deps.settings.rules_config_path).preprocess
    tasks = [_preprocess_one(c, cfg, deps) for c in state["raw_chats"]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    kept: list[PreprocessedChat] = []
    errors: list[str] = []
    for chat, res in zip(state["raw_chats"], results):
        if isinstance(res, Exception):
            errors.append(f"preprocess:{chat['chat_id']}:{res!r}")
        elif res is not None:
            kept.append(res)

    log.info(
        "preprocess.done",
        run_id=state["run_id"],
        input=len(state["raw_chats"]),
        kept=len(kept),
        errors=len(errors),
    )
    return {
        "preprocessed_chats": kept,
        "errors": errors,
        "metrics": {**state.get("metrics", {}), "after_preprocess": len(kept)},
    }
