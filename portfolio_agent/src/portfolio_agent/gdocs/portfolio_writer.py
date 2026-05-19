"""
Renders PortfolioEntry objects into each user's own Google Doc.

Model B: per-user OAuth. For each (user_email, year_month) group the node:
  1. Looks up the user's google_folder_id + google_token_json from DB.
  2. Builds a GoogleDocsClient scoped to that user's credentials.
  3. Gets-or-creates a doc in the user's own Drive folder.
  4. Writes all entries for that group (newest first).
  5. Persists the refreshed token back to DB if it changed.
"""
from __future__ import annotations

import structlog

from portfolio_agent.deps import Deps
from portfolio_agent.gdocs.auth import build_creds_for_user
from portfolio_agent.gdocs.client import GoogleDocsClient
from portfolio_agent.state import PortfolioState
from portfolio_agent.types import PortfolioEntry

log = structlog.get_logger()

_DIVIDER_THIN  = "─" * 60
_DIVIDER_THICK = "═" * 60


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_entry_block(e: PortfolioEntry) -> str:
    highlights = "\n".join(f'  > "{h}"' for h in e.user_highlights)
    return (
        f"{e.conversation_title}     [Score: {e.overall_score}/100]\n"
        f"{_DIVIDER_THIN}\n"
        f"What you did:\n"
        f"{highlights}\n\n"
        f"What the assistant contributed:\n"
        f"  {e.assistant_summary}\n\n"
        f"Why this matters:\n"
        f"{e.why_it_matters}\n\n"
        f"Source: {e.citation}\n"
        f"{_DIVIDER_THICK}\n\n"
    )


def build_entry_requests(entries: list[PortfolioEntry], doc_end_index: int) -> list[dict]:
    """
    batchUpdate requests to append all entries at the end of the doc, oldest first.

    Entries are written in chronological order (oldest first) so the doc reads
    as a natural timeline. Each entry advances the cursor for the next one —
    no existing content is shifted.

    Per entry: insertText + createNamedRange + HEADING_2 on title line.
    """
    requests: list[dict] = []
    cursor = doc_end_index

    for e in sorted(entries, key=lambda x: x.generated_at):
        block     = render_entry_block(e)
        end_idx   = cursor + len(block)
        title_end = cursor + len(e.conversation_title) + 1

        requests.append({
            "insertText": {"location": {"index": cursor}, "text": block}
        })
        requests.append({
            "createNamedRange": {
                "name": f"entry_{e.chat_id}",
                "range": {"startIndex": cursor, "endIndex": end_idx},
            }
        })
        requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": cursor, "endIndex": title_end},
                "paragraphStyle": {"namedStyleType": "HEADING_2"},
                "fields": "namedStyleType",
            }
        })
        cursor = end_idx

    return requests


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def write_to_gdoc_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    # Group entries by (user_email, YYYY-MM) — one doc per user per month.
    groups: dict[tuple[str, str], list[PortfolioEntry]] = {}
    for e in state["generated_entries"]:
        ym = e.generated_at.strftime("%Y-%m")
        groups.setdefault((e.user_email, ym), []).append(e)

    written: list[PortfolioEntry] = []
    errors:  list[str]            = []

    for (email, ym), entries in groups.items():
        try:
            # 1. Fetch per-user Google config from DB.
            config = await deps.ops_repo.get_user_google_config(email)
            if not config or not config.get("google_token_json"):
                errors.append(
                    f"write:no_google_config:{email} — "
                    "run `portfolio-agent auth-user --email {email}` first"
                )
                log.warning("write.no_google_config", email=email)
                continue

            original_token = config["google_token_json"]
            folder_id      = config["google_folder_id"]

            # 2. Build per-user credentials (auto-refreshes if expired).
            creds = build_creds_for_user(
                original_token,
                deps.settings.google_client_secrets_path,
            )

            # 3. Persist refreshed token if it changed.
            refreshed_token = creds.to_json()
            if refreshed_token != original_token:
                await deps.ops_repo.update_user_google_token(
                    user_email=email, google_token_json=refreshed_token
                )

            # 4. Build per-user Google Docs client.
            gdocs = GoogleDocsClient.for_user(creds)

            # 5. Get-or-create the user's portfolio doc for this month.
            def _creator(email: str = email, ym: str = ym, folder_id: str = folder_id) -> str:
                return gdocs.create_doc(
                    title=f"Portfolio - {email} - {ym}",
                    parent_folder_id=folder_id,
                )

            doc_id = await deps.ops_repo.get_or_create_doc(
                user_email=email, year_month=ym, creator=_creator,
            )

            # 6. Write all entries in one batchUpdate call, appended at the end.
            doc_end = gdocs.get_doc_end_index(doc_id)
            requests = build_entry_requests(entries, doc_end_index=doc_end)
            gdocs.batch_update(doc_id, requests)

            # 7. Record each entry in the operational DB.
            written_this_user: list[PortfolioEntry] = []
            for e in entries:
                anchor = f"entry_{e.chat_id}"
                e.google_doc_id   = doc_id
                e.entry_anchor_id = anchor
                await deps.ops_repo.record_entry(
                    chat_id=e.chat_id, doc_id=doc_id,
                    anchor_id=anchor, overall_score=e.overall_score,
                )
                written.append(e)
                written_this_user.append(e)

            log.info(
                "write.doc_done",
                run_id=state["run_id"],
                email=email, ym=ym, doc_id=doc_id, entries=len(entries),
            )

            # Notify the user about their new portfolio entries.
            await deps.notifier.send_portfolio_digest(
                user_email=email,
                entries=written_this_user,
                doc_id=doc_id,
                year_month=ym,
            )

        except Exception as ex:
            errors.append(f"write:{email}/{ym}:{ex!r}")
            log.error("write.doc_failed", email=email, ym=ym, err=repr(ex))

    return {
        "written_entries": written,
        "errors": errors,
        "metrics": {**state.get("metrics", {}), "written_count": len(written)},
    }
