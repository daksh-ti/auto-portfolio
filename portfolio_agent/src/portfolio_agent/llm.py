"""
Thin async LLM wrapper around ChatOpenAI with:
  - per-instance concurrency semaphore
  - structured output via OpenAI JSON schema mode
  - automatic retry on transient errors
"""
from __future__ import annotations

import asyncio
import warnings
from typing import Type, TypeVar

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import APIError, APITimeoutError, RateLimitError
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

T = TypeVar("T", bound=BaseModel)
log = structlog.get_logger()


class LLMClient:
    """One client per (model, temperature) pair, with its own concurrency budget."""

    def __init__(
        self,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        api_key: str,
        timeout_s: float,
        concurrency: int,
    ) -> None:
        self._chat = ChatOpenAI(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=api_key,
            timeout=timeout_s,
            max_retries=0,  # tenacity handles retries
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._model = model

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1, max=20),
        retry=retry_if_exception_type((RateLimitError, APIError, APITimeoutError)),
        reraise=True,
    )
    async def invoke_structured(
        self,
        *,
        system: str,
        user: str,
        schema: Type[T],
    ) -> T:
        async with self._sem:
            structured = self._chat.with_structured_output(
                schema,
                method="json_schema",
                strict=True,
            )
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="PydanticSerializationUnexpectedValue",
                    category=UserWarning,
                )
                result = await structured.ainvoke(
                    [SystemMessage(content=system), HumanMessage(content=user)]
                )
            return result  # type: ignore[return-value]
