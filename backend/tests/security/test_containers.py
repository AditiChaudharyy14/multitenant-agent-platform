"""Both images run as a non-root user, and the backend installs from its lockfile."""

from pathlib import Path

import pytest

DOCKERFILES = {"backend": Path("/app/Dockerfile"), "frontend": Path("/repo/frontend/Dockerfile")}
if not DOCKERFILES["frontend"].exists():
    pytest.skip("repo files are not mounted here", allow_module_level=True)


def _final_stage(path: Path) -> list[str]:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    last_from = max(i for i, ln in enumerate(lines) if ln.upper().startswith("FROM "))
    return lines[last_from:]


@pytest.mark.parametrize("image", list(DOCKERFILES))
def test_the_image_runs_as_a_non_root_user(image: str) -> None:
    users = [ln.split()[1] for ln in _final_stage(DOCKERFILES[image]) if ln.upper().startswith("USER ")]
    assert users, f"{image}: no USER instruction, so it runs as root"
    assert users[-1] not in ("root", "0"), users[-1]


def test_the_backend_installs_from_its_lockfile() -> None:
    stage = "\n".join(_final_stage(DOCKERFILES["backend"]))
    assert "requirements.lock" in stage and "pip install --no-cache-dir -r requirements.lock" in stage
