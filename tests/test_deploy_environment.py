from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_TARGET = REPOSITORY_ROOT / "scripts" / "compose_target.sh"
DEPLOY = REPOSITORY_ROOT / "scripts" / "deploy.sh"


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "release"
    root.mkdir()
    (root / ".env").write_text("PIXO_ENVIRONMENT=development\n")
    (root / "docker-compose.yml").write_text("services: {}\n")
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    docker = binary_dir / "docker"
    docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    docker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{binary_dir}:{environment['PATH']}"
    return root, environment


def _run(root: Path, environment: dict[str, str], profile: str):
    return subprocess.run(
        [str(COMPOSE_TARGET), str(root), "ivapp", profile, "config"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_development_compose_never_loads_rds_overlay(tmp_path: Path) -> None:
    root, environment = _fixture(tmp_path)

    result = _run(root, environment, "development")

    assert result.returncode == 0
    assert ".env.target" not in result.stdout
    assert "docker-compose.rds.yml" not in result.stdout


def test_development_compose_refuses_target_credentials(tmp_path: Path) -> None:
    root, environment = _fixture(tmp_path)
    (root / ".env.target").write_text("PIXO_ENVIRONMENT=production\n")

    result = _run(root, environment, "development")

    assert result.returncode != 0
    assert "refuses .env.target" in result.stderr


def test_production_compose_requires_and_loads_rds_overlay(tmp_path: Path) -> None:
    root, environment = _fixture(tmp_path)
    missing = _run(root, environment, "production")
    assert missing.returncode != 0

    (root / ".env.target").write_text("PIXO_ENVIRONMENT=production\n")
    (root / "docker-compose.rds.yml").write_text("services: {}\n")
    result = _run(root, environment, "production")

    assert result.returncode == 0
    assert str(root / ".env.target") in result.stdout
    assert str(root / "docker-compose.rds.yml") in result.stdout
    assert "background-workers" in result.stdout


def test_deploy_requires_an_explicit_environment() -> None:
    result = subprocess.run(
        [str(DEPLOY)], check=False, capture_output=True, text=True
    )

    assert result.returncode == 2
    assert "--environment must be development or production" in result.stderr
