"""pytest plugin: redact credentials from every report pytest prints (see app.redact).

Loaded with `-p app.redact_pytest` (pyproject.toml addopts), so it is active before
tests/conftest.py is imported.
"""

from typing import Any

import pytest

from app.redact import redact


def _clean_report(report: Any) -> None:
    if report.longrepr is not None:
        text = str(report.longrepr)
        clean = redact(text)
        if clean != text:
            report.longrepr = clean  # a plain string is a valid longrepr
    report.sections = [(title, redact(content)) for title, content in report.sections]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any):  # noqa: ANN201
    outcome = yield
    _clean_report(outcome.get_result())


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector: Any):  # noqa: ANN201
    outcome = yield
    _clean_report(outcome.get_result())
