"""Every environment variable the code reads is documented in .env.example, and vice versa.

"Reads" means: a Settings field (pydantic-settings maps `llm_provider` to LLM_PROVIDER), an
os.environ / os.getenv literal in the backend or the eval harness, a ${VAR} interpolated by
docker-compose.yml, a variable the database init scripts use, or process.env.X in the
frontend. A variable set inside docker-compose.yml for a container (not user
configuration) may appear in .env.example as a commented line (`# VAR=...`).

The repo-level files are mounted read-only at /repo by the `test` service; .env itself is
deliberately not mounted (it holds the API key, and tests must never see one).
"""

import re
from pathlib import Path

import pytest

from app.config import Settings

REPO = Path("/repo")
if not (REPO / ".env.example").exists():
    pytest.skip("repo files are not mounted here (see the test service in docker-compose.yml)", allow_module_level=True)

APP = Path(__file__).resolve().parents[2]  # backend/ (mounted at /app)
EVAL = Path("/eval")

_ENV_READ = re.compile(r"""(?:environ(?:\.get)?\(|environ\[|getenv\()\s*["']([A-Z][A-Z0-9_]*)["']""")
_COMPOSE_VAR = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
_SQL_VAR = re.compile(r"\b(APP_[A-Z_]*PASSWORD|POSTGRES_[A-Z]+)\b")
_PROCESS_ENV = re.compile(r"process\.env\.([A-Z][A-Z0-9_]*)")
_EXAMPLE_KEY = re.compile(r"^(?:# )?([A-Z][A-Z0-9_]*)=", re.M)  # `VAR=` or documented `# VAR=`


def _scan(files: list[Path], pattern: re.Pattern[str]) -> set[str]:
    return {m for f in files for m in pattern.findall(f.read_text(encoding="utf-8"))}


def variables_read() -> dict[str, set[str]]:
    py = [*APP.glob("app/**/*.py"), *APP.glob("scripts/**/*.py"), *EVAL.glob("*.py")]
    frontend = [p for p in (REPO / "frontend").rglob("*") if p.suffix in {".ts", ".tsx"} and "node_modules" not in p.parts]
    return {
        "Settings": {name.upper() for name in Settings.model_fields},
        "os.environ": _scan(py, _ENV_READ),
        "docker-compose.yml": _scan([REPO / "docker-compose.yml"], _COMPOSE_VAR),
        "docker/db": _scan(sorted((REPO / "docker/db").glob("*")), _SQL_VAR),
        "frontend": _scan(frontend, _PROCESS_ENV),
    }


def documented() -> set[str]:
    return set(_EXAMPLE_KEY.findall((REPO / ".env.example").read_text(encoding="utf-8")))


def test_every_variable_the_code_reads_is_in_env_example() -> None:
    missing = {src: sorted(names - documented()) for src, names in variables_read().items() if names - documented()}
    assert missing == {}, f"add these to .env.example: {missing}"


def test_every_variable_in_env_example_is_read_somewhere() -> None:
    read = set().union(*variables_read().values())
    assert sorted(documented() - read) == [], "documented in .env.example but nothing reads it"


def test_the_scan_finds_known_reads() -> None:
    """Guards the regexes: if a scan silently found nothing, both tests above would pass."""
    found = variables_read()
    assert {"LLM_PROVIDER", "GEMINI_API_KEY", "SMTP_HOST"} <= found["Settings"]
    assert {"OTEL_EXPORTER_OTLP_ENDPOINT", "EVAL_SEED_NOW"} <= found["os.environ"]
    assert {"POSTGRES_USER", "APP_USER_PASSWORD"} <= found["docker-compose.yml"] | found["docker/db"]
    assert "BACKEND_URL" in found["frontend"]
