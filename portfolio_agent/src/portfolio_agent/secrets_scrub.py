"""
Redacts common secret patterns from text before any LLM call.
Applied in _preprocess_one (raw messages) and again on rendered chat_text
in _analyze_one / _generate_one (defense in depth).
"""
from __future__ import annotations

import re

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:AWS_ACCESS_KEY]"),
    (
        re.compile(r"(?i)aws.{0,20}secret.{0,3}['\"]([0-9a-zA-Z/+=]{40})['\"]"),
        "[REDACTED:AWS_SECRET]",
    ),
    (
        re.compile(
            r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----[\s\S]+?-----END[^-]+-----"
        ),
        "[REDACTED:PRIVATE_KEY]",
    ),
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        "[REDACTED:JWT]",
    ),
    (re.compile(r"ghp_[A-Za-z0-9]{36,}"), "[REDACTED:GITHUB_TOKEN]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED:OPENAI_KEY]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:SLACK_TOKEN]"),
]


def scrub(text: str) -> str:
    for pat, repl in PATTERNS:
        text = pat.sub(repl, text)
    return text
