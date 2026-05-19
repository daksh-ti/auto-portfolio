"""
Fetches unprocessed Google Doc comments from all known portfolio docs.

Per-user OAuth model: we group docs by user_email and build one
GoogleDocsClient per user so we only load credentials once per user per run.
"""
from __future__ import annotations

import json
from datetime import datetime

import structlog

from portfolio_agent.deps import Deps
from portfolio_agent.gdocs.auth import build_creds_for_user
from portfolio_agent.gdocs.client import GoogleDocsClient
from portfolio_agent.state import FeedbackState
from portfolio_agent.types import CommentRecord

log = structlog.get_logger()


def resolve_anchor(
    anchor_blob: str | None,
    named_ranges: dict[str, dict],
    entries_in_doc: dict[str, str],
) -> tuple[str | None, str | None]:
    """
    Map a Drive comment 'anchor' blob to (anchor_name, chat_id).

    Drive returns anchors like '{"r":"...","a":[{"kix.r":...}]}'. We extract
    the integer offsets and find the named range whose range overlaps.
    Returns (None, None) when the anchor can't be resolved (e.g. doc-level
    comments with no precise anchor).
    """
    if not anchor_blob:
        return None, None
    try:
        a = json.loads(anchor_blob)
        offsets = [
            seg.get("kix.r") or seg.get("kix.s")
            for seg in a.get("a", [])
            if isinstance(seg, dict)
        ]
        offsets = [o for o in offsets if isinstance(o, int)]
        if not offsets:
            return None, None
        start = min(offsets)
        end   = max(offsets)
    except Exception:
        return None, None

    for name, group in named_ranges.items():
        for nr in group.get("namedRanges", []):
            for rng in nr.get("ranges", []):
                s = rng.get("startIndex", -1)
                e = rng.get("endIndex",   -1)
                if s <= start and end <= e:
                    return name, entries_in_doc.get(name)
    return None, None


async def fetch_comments_node(state: FeedbackState, deps: Deps) -> FeedbackState:
    since_raw = state.get("since") or ""
    since = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))

    # Build a map: user_email -> [doc_id]
    doc_user_pairs = await deps.ops_repo.list_docs_with_users()
    by_user: dict[str, list[str]] = {}
    for doc_id, user_email in doc_user_pairs:
        by_user.setdefault(user_email, []).append(doc_id)

    all_comments: list[CommentRecord] = []

    for user_email, doc_ids in by_user.items():
        # Build per-user credentials once per user.
        config = await deps.ops_repo.get_user_google_config(user_email)
        if not config or not config.get("google_token_json"):
            log.warning("fetch_comments.no_google_config", email=user_email)
            continue

        original_token = config["google_token_json"]
        creds = build_creds_for_user(
            original_token,
            deps.settings.google_client_secrets_path,
        )
        # Persist refreshed token if needed.
        refreshed = creds.to_json()
        if refreshed != original_token:
            await deps.ops_repo.update_user_google_token(
                user_email=user_email, google_token_json=refreshed
            )

        gdocs = GoogleDocsClient.for_user(creds)

        for doc_id in doc_ids:
            try:
                raw_comments = gdocs.list_comments(doc_id=doc_id, since=since)
                if not raw_comments:
                    await deps.ops_repo.update_last_checked(doc_id)
                    continue

                named_ranges  = gdocs.list_named_ranges(doc_id=doc_id)
                entries_in_doc = await deps.ops_repo.entries_in_doc(doc_id)

                for r in raw_comments:
                    if r.get("resolved"):
                        continue
                    if await deps.ops_repo.is_comment_seen(r["id"]):
                        continue

                    anchor_id, chat_id = resolve_anchor(
                        r.get("anchor"), named_ranges, entries_in_doc
                    )
                    # Drive may omit emailAddress due to privacy settings; fall back
                # to the doc owner's email (the user who owns this portfolio doc).
                author_email = (
                    r["author"].get("emailAddress")
                    or user_email
                )
                all_comments.append(
                        CommentRecord(
                            comment_id=r["id"],
                            google_doc_id=doc_id,
                            entry_anchor_id=anchor_id,
                            chat_id=chat_id,
                            author_email=author_email,
                            author_name=r["author"].get("displayName", ""),
                            quoted_text=(r.get("quotedFileContent") or {}).get("value"),
                            body=r["content"],
                            created_at=datetime.fromisoformat(
                                r["createdTime"].replace("Z", "+00:00")
                            ),
                            resolved=False,
                        )
                    )

                await deps.ops_repo.update_last_checked(doc_id)

            except Exception as ex:
                log.warning(
                    "fetch_comments.doc_failed",
                    doc_id=doc_id,
                    email=user_email,
                    err=repr(ex),
                )

    log.info(
        "fetch_comments.done",
        run_id=state.get("run_id"),
        total=len(all_comments),
    )
    return {
        "fetched_comments": all_comments,
        "metrics": {**state.get("metrics", {}), "fetched_count": len(all_comments)},
    }
