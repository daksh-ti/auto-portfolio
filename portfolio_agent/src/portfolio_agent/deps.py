from __future__ import annotations

from dataclasses import dataclass

from portfolio_agent.db.cursor_chats import ChatRepo
from portfolio_agent.db.engine import build_engines
from portfolio_agent.db.operational import OperationalRepo
from portfolio_agent.db.users import UsersRepo
from portfolio_agent.llm import LLMClient
from portfolio_agent.notifications import EmailNotifier
from portfolio_agent.settings import Settings, get_settings


@dataclass
class Deps:
    settings: Settings
    chat_repo: ChatRepo
    users_repo: UsersRepo
    ops_repo: OperationalRepo
    notifier: EmailNotifier
    # No global gdocs client — each user gets their own in portfolio_writer.py
    llm_preprocess: LLMClient
    llm_analyze: LLMClient
    llm_generate: LLMClient
    llm_feedback: LLMClient


async def build_deps() -> Deps:
    s = get_settings()
    source_eng, ops_eng = await build_engines(s)

    def _llm(model: str, temp: float) -> LLMClient:
        return LLMClient(
            model=model,
            temperature=temp,
            max_tokens=s.openai_max_tokens,
            api_key=s.openai_api_key,
            timeout_s=s.openai_request_timeout_s,
            concurrency=s.openai_max_concurrency,
        )

    return Deps(
        settings=s,
        chat_repo=ChatRepo(source_eng),
        users_repo=UsersRepo(source_eng),  # same engine — different tables, same DB
        ops_repo=OperationalRepo(ops_eng),
        notifier=EmailNotifier(s),
        llm_preprocess=_llm(s.openai_model_preprocess, s.openai_temperature_preprocess),
        llm_analyze=_llm(s.openai_model_analyze, s.openai_temperature_analyze),
        llm_generate=_llm(s.openai_model_generate, s.openai_temperature_generate),
        llm_feedback=_llm(s.openai_model_feedback, s.openai_temperature_feedback),
    )
