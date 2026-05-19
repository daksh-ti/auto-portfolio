"""
FeedbackGraph nodes.

Pipeline:
  fetch_comments  →  classify_comments  →  route_feedback
                                         →  persist_feedback
                                         →  update_rules_config
"""
from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from portfolio_agent.deps import Deps
from portfolio_agent.prompts import read_text, render
from portfolio_agent.rules import (
    apply_weight_deltas,
    clamp_weight,
    load_rules,
    save_rules,
)
from portfolio_agent.state import FeedbackState
from portfolio_agent.types import ClassifiedFeedback, CommentRecord, FeedbackTarget

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Structured output schema for the classification LLM call
# ---------------------------------------------------------------------------

class WeightDelta(BaseModel):
    """A single rule weight adjustment suggested by the LLM."""
    rule_id: str
    delta: float = Field(ge=-0.2, le=0.2, description="Suggested weight change for this rule")


class ClassifyOutput(BaseModel):
    target: FeedbackTarget
    sentiment: Literal["positive", "negative", "neutral", "mixed"]
    rule_ids_touched: list[str] = Field(
        default_factory=list,
        description="Rule IDs this comment relates to (may be empty)",
    )
    suggested_weight_deltas: list[WeightDelta] = Field(
        default_factory=list,
        description="Per-rule weight adjustments. Omit unless admin clearly signals mis-weighting.",
    )
    actionable_takeaway: str = Field(
        min_length=10,
        max_length=300,
        description="One sentence the system should learn for next time",
    )


# ---------------------------------------------------------------------------
# Node 2: classify_comments
# ---------------------------------------------------------------------------

async def classify_comments_node(state: FeedbackState, deps: Deps) -> FeedbackState:
    rc = load_rules(deps.settings.rules_config_path)
    tasks = [_classify_one(c, rc, deps) for c in state["fetched_comments"]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out, errors = [], []
    for c, res in zip(state["fetched_comments"], results):
        if isinstance(res, Exception):
            errors.append(f"classify:{c.comment_id}:{res!r}")
            log.warning("classify.failed", comment_id=c.comment_id, err=repr(res))
        else:
            out.append(res)

    log.info(
        "classify.done",
        run_id=state.get("run_id"),
        classified=len(out),
        errors=len(errors),
    )
    return {
        "classified": out,
        "errors": errors,
        "metrics": {**state.get("metrics", {}), "classified_count": len(out)},
    }


async def _classify_one(
    c: CommentRecord, rc, deps: Deps
) -> ClassifiedFeedback:
    raw: ClassifyOutput = await deps.llm_feedback.invoke_structured(
        system=read_text("feedback_system.txt"),
        user=render(
            "feedback_user.j2",
            rules=rc.rules,
            author_name=c.author_name,
            author_email=c.author_email,
            created_at=c.created_at.isoformat(),
            attached_to_hint="UNKNOWN",   # simplified; LLM reads quoted_text directly
            quoted_text=c.quoted_text,
            body=c.body,
        ),
        schema=ClassifyOutput,
    )

    # Server-side safety: ignore unknown rule IDs, clamp deltas.
    valid_ids = {r.id for r in rc.rules}
    deltas = {
        wd.rule_id: max(-0.2, min(0.2, wd.delta))
        for wd in raw.suggested_weight_deltas
        if wd.rule_id in valid_ids
    }
    return ClassifiedFeedback(
        comment=c,
        target=raw.target,
        sentiment=raw.sentiment,
        rule_ids_touched=[r for r in raw.rule_ids_touched if r in valid_ids],
        suggested_weight_delta=deltas,
        actionable_takeaway=raw.actionable_takeaway,
    )


# ---------------------------------------------------------------------------
# Node 3: route_feedback  (write to calibration tables)
# ---------------------------------------------------------------------------

async def route_feedback_node(state: FeedbackState, deps: Deps) -> FeedbackState:
    for f in state.get("classified", []):
        c = f.comment
        kwargs = dict(
            feedback_id=c.comment_id,
            chat_id=c.chat_id,
            comment_body=c.body,
            sentiment=f.sentiment,
            takeaway=f.actionable_takeaway,
        )
        if f.target == FeedbackTarget.PROMPT:
            await deps.ops_repo.insert_analyzer_feedback(**kwargs)
        elif f.target == FeedbackTarget.ENTRY_TEXT:
            await deps.ops_repo.insert_generator_feedback(**kwargs)
        # FeedbackTarget.UNKNOWN: skip calibration tables; goes to feedback_log only.

    log.info("route_feedback.done", run_id=state.get("run_id"))
    return {}


# ---------------------------------------------------------------------------
# Node 4: persist_feedback  (write everything to feedback_log)
# ---------------------------------------------------------------------------

async def persist_feedback_node(state: FeedbackState, deps: Deps) -> FeedbackState:
    classified = state.get("classified") or []
    if not classified:
        return {"persisted_to_db": []}

    persisted: list[str] = []
    for f in classified:
        c = f.comment
        if await deps.ops_repo.is_comment_seen(c.comment_id):
            continue
        await deps.ops_repo.insert_feedback_log(
            comment_id=c.comment_id,
            doc_id=c.google_doc_id,
            entry_anchor_id=c.entry_anchor_id,
            chat_id=c.chat_id,
            author_email=c.author_email,
            author_name=c.author_name,
            quoted_text=c.quoted_text,
            comment_body=c.body,
            target=f.target.value,
            sentiment=f.sentiment,
            rule_ids_touched=f.rule_ids_touched,
            suggested_deltas=f.suggested_weight_delta,
            actionable_takeaway=f.actionable_takeaway,
            created_at=c.created_at,
        )
        persisted.append(c.comment_id)

    log.info(
        "persist_feedback.done",
        run_id=state.get("run_id"),
        persisted=len(persisted),
    )
    return {
        "persisted_to_db": persisted,
        "metrics": {**state.get("metrics", {}), "persisted_count": len(persisted)},
    }


# ---------------------------------------------------------------------------
# Node 5: update_rules_config
# ---------------------------------------------------------------------------

async def update_rules_config_node(state: FeedbackState, deps: Deps) -> FeedbackState:
    rc = load_rules(deps.settings.rules_config_path)
    classified = state.get("classified") or []

    # Aggregate suggested deltas across all classified comments.
    sums: dict[str, float]          = {}
    driving: dict[str, list[str]]   = {}
    for f in classified:
        for rid, d in f.suggested_weight_delta.items():
            sums[rid] = sums.get(rid, 0.0) + d
            driving.setdefault(rid, []).append(f.comment.comment_id)

    if not sums:
        log.info("update_rules_config.no_changes", run_id=state.get("run_id"))
        return {"rules_updated": False, "new_rules_version": None}

    applied: dict[str, float]   = {}
    pending: dict[str, float]   = {}
    max_delta   = deps.settings.max_weight_delta_per_run
    manual_thr  = deps.settings.manual_approval_threshold

    for rid, total in sums.items():
        capped = max(-max_delta, min(max_delta, total))
        if abs(total) >= manual_thr:
            pending[rid] = capped
        else:
            applied[rid] = capped

    # Queue large changes for manual approval.
    for rid, w in pending.items():
        current = next((r.weight for r in rc.rules if r.id == rid), None)
        if current is None:
            continue
        proposed = clamp_weight(
            current + w,
            weight_min=deps.settings.weight_min,
            weight_max=deps.settings.weight_max,
        )
        await deps.ops_repo.record_pending_change(
            rule_id=rid,
            current_weight=current,
            proposed_weight=proposed,
            driving_comment_ids=driving[rid],
        )
        log.info(
            "update_rules_config.pending",
            rule_id=rid,
            current=current,
            proposed=proposed,
        )

    if not applied:
        return {"rules_updated": False, "new_rules_version": None}

    # Auto-apply small changes.
    new_rc = apply_weight_deltas(
        rc,
        applied,
        driving_comment_ids=driving,
        reason=f"Auto-applied from feedback run {state.get('run_id', 'unknown')}.",
        weight_min=deps.settings.weight_min,
        weight_max=deps.settings.weight_max,
    )
    save_rules(deps.settings.rules_config_path, new_rc)
    log.info(
        "update_rules_config.applied",
        run_id=state.get("run_id"),
        deltas=applied,
        new_version=new_rc.version,
    )

    if deps.settings.rules_git_repo_path:
        from portfolio_agent.rules import git_commit
        try:
            git_commit(
                deps.settings.rules_git_repo_path,
                message=f"rules: v{new_rc.version}; deltas={applied}",
            )
        except Exception as ex:
            log.warning("update_rules_config.git_commit_failed", err=repr(ex))

    if deps.settings.slack_webhook_url:
        from portfolio_agent.scheduler import _notify_slack
        _notify_slack(
            deps.settings.slack_webhook_url,
            text=f"rules_config v{new_rc.version} applied: {applied}",
        )

    return {"rules_updated": True, "new_rules_version": new_rc.version}
