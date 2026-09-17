from __future__ import annotations

import inspect
from pathlib import Path

from app import worker
from app.config import Settings


def test_rebuild_interval_config_default() -> None:
    field = Settings.model_fields["recommend_pool_rebuild_interval_seconds"]
    assert field.default == 1800


def test_worker_schedules_pool_rebuild() -> None:
    src = Path(inspect.getsourcefile(worker)).read_text(encoding="utf-8")
    # The long-lived worker owns periodic interval scheduling, not a per-loop call.
    assert "recommend_pool_rebuild_interval_seconds" in src
    assert "rebuild_once()" in src
    assert "next_rebuild_at" in src


def test_main_rebuilds_pool_on_startup(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_rebuild() -> None:
        calls["n"] += 1

    # Avoid touching DB/HTTP task processors.
    monkeypatch.setattr(worker, "process_next_upload_normalization", lambda *a, **k: False)
    monkeypatch.setattr(worker, "process_next_source_generation", lambda *a, **k: False)
    monkeypatch.setattr(worker, "process_next_creator_version", lambda *a, **k: False)
    monkeypatch.setattr(worker, "process_next_expired_source", lambda *a, **k: False)
    monkeypatch.setattr(worker, "rebuild_once", fake_rebuild)
    # Stop the infinite loop after the first iteration.
    monkeypatch.setattr(worker.time, "sleep", lambda *_a, **_k: setattr(worker, "_stop", True))
    # Skip signal/env-contract side effects.
    monkeypatch.setattr(worker, "validate_environment_contract", lambda *_a, **_k: None)
    monkeypatch.setattr(worker.signal, "signal", lambda *_a, **_k: None)

    worker._stop = False
    try:
        assert worker.main() == 0
    finally:
        worker._stop = False
    assert calls["n"] == 1  # built once right after startup
