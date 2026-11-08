"""`make up` runs production mode by default; hot reload is opt-in (`make dev`).

Production: the frontend serves a `next build` (no per-page compile on first visit, and the
CSP is the production one) and uvicorn runs without --reload. APP_MODE=dev switches both.
"""

from pathlib import Path

import pytest
import yaml

COMPOSE = Path("/repo/docker-compose.yml")
if not COMPOSE.exists():
    pytest.skip("repo files are not mounted here", allow_module_level=True)

SERVICES = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _cmd(service: str) -> str:
    cmd = SERVICES[service]["command"]
    return cmd if isinstance(cmd, str) else " ".join(cmd)


@pytest.mark.parametrize("service", ["backend", "frontend"])
def test_app_mode_defaults_to_production(service: str) -> None:
    assert SERVICES[service]["environment"]["APP_MODE"] == "${APP_MODE:-prod}"


def test_frontend_builds_and_serves_in_production_and_only_dev_mode_runs_next_dev() -> None:
    cmd = _cmd("frontend")
    assert "npm run build" in cmd and "npm run start" in cmd
    assert 'if [ "$$APP_MODE" = dev ]; then exec npm run dev' in cmd


def test_backend_reloads_only_in_dev_mode() -> None:
    cmd = _cmd("backend")
    assert "--reload" in cmd and '"$$APP_MODE" = dev' in cmd


def test_the_frontend_healthcheck_allows_for_the_production_build() -> None:
    assert SERVICES["frontend"]["healthcheck"]["start_period"] == "300s"
