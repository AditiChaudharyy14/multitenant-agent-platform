"""Test and eval error output never shows a database password or an API key.

A failing assertion in tests/conftest.py once printed the full app_owner URL, password
included. app.redact masks credentials in URLs and the configured secret values, and is
used by the pytest plugin app.redact_pytest and by the eval runner.
"""

import pytest

from app.redact import redact

URL = "postgresql+asyncpg://app_owner:s3cr3t-Pa55word@db-ephemeral:5432/app_test"


def test_password_in_a_url_is_masked() -> None:
    out = redact(f"connect failed: {URL!r}")
    assert "s3cr3t-Pa55word" not in out
    assert "postgresql+asyncpg://app_owner:***@db-ephemeral:5432/app_test" in out


def test_several_urls_and_odd_quoting() -> None:
    text = f'"{URL}" and postgres://u:p%40ss@h/db, also <{URL}>'
    out = redact(text)
    assert "s3cr3t" not in out and "p%40ss" not in out


def test_configured_secret_values_are_masked_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_OWNER_PASSWORD", "loose-password-123")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKEFAKEFAKEFAKE")
    out = redact("pw=loose-password-123 key AIzaFAKEFAKEFAKEFAKE")
    assert "loose-password-123" not in out and "AIzaFAKE" not in out


def test_text_without_secrets_is_unchanged() -> None:
    plain = "assert 'db' == 'db-ephemeral' at http://localhost:3000/api"
    assert redact(plain) == plain


def test_pytest_failure_output_is_redacted(pytester: pytest.Pytester) -> None:
    """End to end: a failing test whose assertion and stdout contain a DB URL."""
    pytester.makepyfile(f'''
        def test_leaks():
            url = "{URL}"
            print("captured:", url)
            assert url.endswith("/elsewhere")
    ''')
    result = pytester.runpytest_subprocess("-p", "app.redact_pytest")
    result.assert_outcomes(failed=1)
    out = result.stdout.str() + result.stderr.str()
    assert "s3cr3t-Pa55word" not in out
    assert "app_owner:***@db-ephemeral" in out
