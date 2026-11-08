"""Keep credentials out of test and eval output.

redact(text) masks the password in any `scheme://user:password@host` URL and the literal
values of the secret settings (database passwords, API keys), wherever they appear.

Used two ways:
- by the pytest plugin app.redact_pytest (`-p app.redact_pytest`, set in pyproject.toml):
  failure reports, captured output and collection errors are redacted before pytest
  prints them. Being a plugin, it is active before tests/conftest.py is imported;
- by the eval runner, for its printed lines, logs, tracebacks and results files.

Why: a failing assertion once printed a full DATABASE_URL_OWNER, password included.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from typing import Any

MASK = "***"
# user:password@ after a scheme. The password stops at the last '@' before the host, and
# never crosses whitespace or quotes (so two URLs on one line are masked separately).
_URL_CREDENTIALS = re.compile(r"(?P<pre>\b[a-z][a-z0-9+.\-]*://[^\s:/@'\"<>]+:)[^\s'\"<>]+?(?=@[^\s@'\"<>]+)", re.I)
# Settings whose values are secrets (matched by name, so new ones are covered).
_SECRET_VAR = re.compile(r"(PASSWORD|_API_KEY|SECRET|TOKEN)$")


def _secret_values() -> list[str]:
    values = {v for k, v in os.environ.items() if _SECRET_VAR.search(k) and len(v) >= 6}
    return sorted(values, key=len, reverse=True)  # longest first: no partial leftovers


def redact(text: str) -> str:
    text = _URL_CREDENTIALS.sub(lambda m: m.group("pre") + MASK, text)
    for value in _secret_values():
        text = text.replace(value, MASK)
    return text


def redact_obj(value: Any) -> Any:
    """A JSON-like structure with every string redacted (for results files)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value


# ───────────── logging and uncaught exceptions (eval runner) ─────────────


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def install_output_redaction() -> None:
    """Redact every log line (tracebacks included) and an uncaught exception's report."""
    for handler in logging.getLogger().handlers:
        if type(handler.formatter).__name__ == "JsonFormatter":
            continue  # app.logging_setup's formatter already redacts, and must stay JSON
        handler.setFormatter(RedactingFormatter(handler.formatter._fmt if handler.formatter else None))

    def hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        sys.stderr.write(redact("".join(traceback.format_exception(exc_type, exc, tb))))

    sys.excepthook = hook
