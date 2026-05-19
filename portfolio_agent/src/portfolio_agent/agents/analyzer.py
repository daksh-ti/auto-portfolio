from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from pydantic import BaseModel, Field

from portfolio_agent.deps import Deps
from portfolio_agent.prompts import read_text, render
from portfolio_agent.rules import load_rules
from portfolio_agent.secrets_scrub import scrub
from portfolio_agent.state import PortfolioState
from portfolio_agent.types import AnalyzedChat, PreprocessedChat, RuleScore

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# LLM output schema
# ---------------------------------------------------------------------------

class AnalyzeOutput(BaseModel):
    rule_scores: list[RuleScore]
    overall_summary: str = Field(min_length=20, max_length=600)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def render_chat_text(c: PreprocessedChat) -> str:
    lines: list[str] = []
    for m in c.messages:
        ts = m.get("timestamp")
        ts_str = ts.isoformat() if isinstance(ts, datetime) else str(ts or "")
        lines.append(f"[{m.get('role', 'unknown')} @ {ts_str}]")
        lines.append(scrub(m.get("content", "")))
        lines.append("")
    return "\n".join(lines)


def compute_overall(rule_scores: list[RuleScore]) -> int:
    total_w = sum(r.weight for r in rule_scores) or 1.0
    weighted = sum(r.score * r.weight for r in rule_scores) / total_w
    return max(10, min(100, round(weighted)))


def ensure_all_rules(rule_scores: list[RuleScore], rules: list) -> list[RuleScore]:
    """Guarantee one score per rule; fills missing ones with score=50."""
    by_id = {r.rule_id: r for r in rule_scores}
    fixed: list[RuleScore] = []
    for r in rules:
        if r.id in by_id:
            fixed.append(by_id[r.id])
        else:
            log.warning("analyze.missing_rule", rule_id=r.id)
            fixed.append(
                RuleScore(
                    rule_id=r.id,
                    rule_name=r.name,
                    score=50,
                    weight=r.weight,
                    justification="(missing in LLM output; defaulted to 50)",
                )
            )
    return fixed


# ---------------------------------------------------------------------------
# Per-chat helper
# ---------------------------------------------------------------------------

async def _analyze_one(
    c: PreprocessedChat,
    rc: object,
    calibration: list[dict],
    run_id: str,
    deps: Deps,
) -> AnalyzedChat:
    chat_text = render_chat_text(c)
    out: AnalyzeOutput = await deps.llm_analyze.invoke_structured(
        system=read_text("analyze_system.txt"),
        user=render(
            "analyze_user.j2",
            rules=rc.rules,  # type: ignore[attr-defined]
            calibration_examples=calibration,
            chat_text=chat_text,
        ),
        schema=AnalyzeOutput,
    )
    rule_scores = ensure_all_rules(out.rule_scores, rc.rules)  # type: ignore[attr-defined]
    overall = compute_overall(rule_scores)

    analyzed = AnalyzedChat(
        chat_id=c.chat_id,
        user_id=c.user_id,
        user_email=c.user_email,
        preprocessed=c,
        rule_scores=rule_scores,
        overall_score=overall,
        analysis_summary=out.overall_summary,
        rules_version=rc.version,  # type: ignore[attr-defined]
        analyzed_at=datetime.now(timezone.utc),
    )

    await deps.ops_repo.record_analysis(
        chat_id=c.chat_id,
        run_id=run_id,
        rules_version=rc.version,  # type: ignore[attr-defined]
        rule_scores=[r.model_dump() for r in rule_scores],
        overall_score=overall,
    )
    return analyzed


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def analyze_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    rc = load_rules(deps.settings.rules_config_path)
    calibration = await deps.ops_repo.recent_analyzer_feedback(limit=5)

    tasks = [
        _analyze_one(c, rc, calibration, state["run_id"], deps)
        for c in state["preprocessed_chats"]
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    analyzed: list[AnalyzedChat] = []
    errors: list[str] = []
    for c, res in zip(state["preprocessed_chats"], results):
        if isinstance(res, Exception):
            errors.append(f"analyze:{c.chat_id}:{res!r}")
        else:
            analyzed.append(res)  # type: ignore[arg-type]

    log.info(
        "analyze.done",
        run_id=state["run_id"],
        analyzed=len(analyzed),
        errors=len(errors),
    )
    return {
        "analyzed_chats": analyzed,
        "errors": errors,
        "rules_version": rc.version,
        "metrics": {**state.get("metrics", {}), "analyzed_count": len(analyzed)},
    }


def threshold_gate_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    rc = load_rules(deps.settings.rules_config_path)
    qualifying = [
        c for c in state["analyzed_chats"] if c.overall_score >= rc.threshold
    ]
    log.info(
        "threshold_gate.done",
        run_id=state["run_id"],
        threshold=rc.threshold,
        qualifying=len(qualifying),
        total=len(state["analyzed_chats"]),
    )
    return {
        "qualifying_chats": qualifying,
        "metrics": {
            **state.get("metrics", {}),
            "threshold": rc.threshold,
            "qualifying_count": len(qualifying),
        },
    }
