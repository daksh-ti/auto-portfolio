from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from pydantic import BaseModel, Field

from portfolio_agent.agents.analyzer import render_chat_text
from portfolio_agent.deps import Deps
from portfolio_agent.prompts import read_text, render
from portfolio_agent.state import PortfolioState
from portfolio_agent.types import AnalyzedChat, PortfolioEntry

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# LLM output schema
# ---------------------------------------------------------------------------

class GenerateOutput(BaseModel):
    conversation_title: str = Field(min_length=6, max_length=80)
    user_highlights: list[str] = Field(
        min_length=1,
        max_length=4,
        description="2-4 key user messages — paraphrased or lightly quoted, ≤80 words each. No assistant content.",
    )
    assistant_summary: str = Field(
        min_length=20,
        max_length=200,
        description="1-2 sentences summarising what the assistant contributed.",
    )
    why_it_matters: str = Field(min_length=80, max_length=800)


# ---------------------------------------------------------------------------
# Per-chat helper
# ---------------------------------------------------------------------------

async def _generate_one(
    c: AnalyzedChat,
    calibration: list[dict],
    deps: Deps,
) -> PortfolioEntry:
    # Top 3 rule scores by weighted contribution (score × weight).
    top = sorted(c.rule_scores, key=lambda r: r.score * r.weight, reverse=True)[:3]
    chat_text = render_chat_text(c.preprocessed)

    out: GenerateOutput = await deps.llm_generate.invoke_structured(
        system=read_text("generate_system.txt"),
        user=render(
            "generate_user.j2",
            chat_text=chat_text,
            top_rule_scores=top,
            overall_score=c.overall_score,
            calibration_examples=calibration,
        ),
        schema=GenerateOutput,
    )

    return PortfolioEntry(
        chat_id=c.chat_id,
        user_id=c.user_id,
        user_email=c.user_email,
        overall_score=c.overall_score,
        conversation_title=out.conversation_title,
        user_highlights=out.user_highlights,
        assistant_summary=out.assistant_summary,
        why_it_matters=out.why_it_matters,
        citation=(
            f"chat_id={c.chat_id} · started "
            f"{c.preprocessed.started_at.isoformat()} · "
            f"project={c.preprocessed.project_id or 'n/a'}"
        ),
        rule_scores=c.rule_scores,
        generated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def generate_entries_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    calibration = await deps.ops_repo.recent_generator_feedback(limit=5)
    tasks = [_generate_one(c, calibration, deps) for c in state["qualifying_chats"]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    entries: list[PortfolioEntry] = []
    errors: list[str] = []
    for c, res in zip(state["qualifying_chats"], results):
        if isinstance(res, Exception):
            errors.append(f"generate:{c.chat_id}:{res!r}")
        else:
            entries.append(res)  # type: ignore[arg-type]

    log.info(
        "generate.done",
        run_id=state["run_id"],
        generated=len(entries),
        errors=len(errors),
    )
    return {
        "generated_entries": entries,
        "errors": errors,
        "metrics": {**state.get("metrics", {}), "generated_count": len(entries)},
    }
