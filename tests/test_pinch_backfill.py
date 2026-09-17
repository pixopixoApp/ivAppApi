import hashlib
from copy import deepcopy

import pytest
from sqlalchemy import (
    JSON,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    select,
)

from app.pinch_backfill import backfill, json_hash, repair_document


def test_all_persisted_shapes_are_explicit_and_other_behavior_is_unchanged():
    original = {"source": {"gesture": "pinch", "gate_at_ms": 3000, "outcomes": [{"target": "B"}]},
                "runtime": {"type": "pinch", "offset_time_ms": 3000, "detection": {"min_scale_delta": .2, "response_window_ms": 1200}},
                "story": {"interaction_type": "pinch", "branch_start_ms": 3000},
                "legacy": {"primary": {"signal": "pointer.pinch"}},
                "out": {"gesture": "pinch", "pinch_direction": "outward"},
                "tap": {"gesture": "tap"}, "video": "https://cdn.test/immutable.mp4"}
    baseline = deepcopy(original)
    repaired, stats = repair_document(original)
    assert original == baseline
    assert stats == {"pinch_nodes": 5, "added": 4, "invalid": []}
    assert repaired["source"]["pinch_direction"] == repaired["runtime"]["detection"]["pinch_direction"] == "inward"
    assert repaired["out"]["pinch_direction"] == "outward"
    again, stats = repair_document(repaired)
    assert again == repaired and stats["added"] == 0
    for node in [repaired["source"], repaired["runtime"]["detection"], repaired["story"], repaired["legacy"]["primary"]]:
        node.pop("pinch_direction")
    assert repaired == baseline


def test_invalid_direction_does_not_get_silently_replaced():
    repaired, stats = repair_document({"gesture": "pinch", "pinch_direction": "both"})
    assert repaired["pinch_direction"] == "both" and stats["invalid"]


def test_database_dry_run_apply_checksums_and_idempotency():
    engine = create_engine("sqlite://")
    meta = MetaData()
    versions = Table("run_versions", meta, Column("id", Integer, primary_key=True), Column("bundle_sha256", String))
    docs = Table("run_version_documents", meta, Column("id", Integer, primary_key=True), Column("run_version_id", Integer), Column("relative_path", String), Column("payload", JSON), Column("content_sha256", String), Column("size_bytes", Integer))
    meta.create_all(engine)
    source = {"gesture": "pinch", "gate_at_ms": 1000}
    sha, size = json_hash(source)
    with engine.begin() as conn:
        conn.execute(versions.insert().values(id=1, bundle_sha256="original"))
        conn.execute(docs.insert().values(id=1, run_version_id=1, relative_path="timeline.json", payload=source, content_sha256=sha, size_bytes=size))
    assert backfill(engine)["added"] == 1
    with engine.connect() as conn:
        assert conn.execute(select(docs.c.payload)).scalar() == source
    report = backfill(engine, apply=True)
    assert report["changed_rows"] == report["bundles_refreshed"] == 1
    with engine.connect() as conn:
        row = conn.execute(select(docs)).mappings().one()
        assert (row["content_sha256"], row["size_bytes"]) == json_hash(row["payload"])
        assert conn.execute(select(versions.c.bundle_sha256)).scalar() == hashlib.sha256(f"timeline.json:{row['content_sha256']}".encode()).hexdigest()
    assert backfill(engine)["added"] == backfill(engine, apply=True)["changed_rows"] == 0
    with pytest.raises(ValueError, match="target"):
        backfill(engine, apply=True, expected_database="production")
