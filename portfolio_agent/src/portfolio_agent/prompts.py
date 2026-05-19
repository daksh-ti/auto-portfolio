"""
Prompt template loader.
  read_text(filename)       — returns raw text of a .txt prompt file
  render(template, **ctx)   — renders a Jinja2 .j2 template with given context
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from portfolio_agent.settings import get_settings


@lru_cache(maxsize=None)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(get_settings().prompts_dir)),
        autoescape=select_autoescape(disabled_extensions=("j2", "txt")),
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def render(template_name: str, **ctx: object) -> str:
    return _env().get_template(template_name).render(**ctx)


def read_text(filename: str) -> str:
    path: Path = get_settings().prompts_dir / filename
    return path.read_text()
