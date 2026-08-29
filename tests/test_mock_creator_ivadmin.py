from __future__ import annotations

from scripts.mock_creator_ivadmin import MockCreatorStore


def test_mock_creator_exposes_all_four_progress_stages(tmp_path) -> None:
    store = MockCreatorStore(tmp_path / "state.json", stage_seconds=1)
    job = store.create_job({"request_id": "request-1", "creation_id": "creation-1"})
    row = store.state["jobs"][job["job_id"]]
    started = float(row["created_epoch"])

    assert [
        store.job_payload(row, now=started + offset)["progress_stage"]
        for offset in (0, 1, 2, 3, 4)
    ] == [
        "validate_video",
        "sample_frames",
        "find_playable_moments",
        "compile_preview",
        "ready",
    ]


def test_mock_normalization_reuses_the_source_cache_object(tmp_path) -> None:
    store = MockCreatorStore(tmp_path / "state.json")
    payload = store.normalization(
        {
            "request_id": "normalize-1",
            "owner_id": "upload-1",
            "source_sha256": "a" * 64,
            "source_size_bytes": 1234,
        }
    )

    assert payload["status"] == "ready"
    assert payload["playable_sha256"] == "a" * 64
    assert payload["playable_size_bytes"] == 1234
