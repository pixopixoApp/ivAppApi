"""Idempotent, direction-only backfill for both production schemas.

Dry run is the default. Invalid existing directions abort the entire write.
No media, thresholds, branching, account data or version compatibility changes.
Run after a consistent database backup; SQLAlchemy handles JSON serialization.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from sqlalchemy import JSON, MetaData, and_, select, update


def repair_document(value):
    repaired = deepcopy(value)
    stats = {"pinch_nodes": 0, "added": 0, "invalid": []}

    def direction(node, path):
        stats["pinch_nodes"] += 1
        current = node.get("pinch_direction")
        if current is None:
            node["pinch_direction"] = "inward"
            stats["added"] += 1
        elif current not in ("inward", "outward"):
            stats["invalid"].append(path)

    def walk(node, path="$", parent_key=""):
        if isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")
        elif isinstance(node, dict):
            if node.get("gesture") == "pinch" or node.get("interaction_type") == "pinch":
                direction(node, path)
            if node.get("type") == "pinch" and ("detection" in node or "offset_time_ms" in node):
                if node.get("detection") is None:
                    node["detection"] = {}
                if not isinstance(node["detection"], dict):
                    stats["invalid"].append(f"{path}.detection")
                else:
                    direction(node["detection"], f"{path}.detection")
            if node.get("signal") == "pointer.pinch":
                direction(node, path)
            for key, child in list(node.items()):
                if isinstance(child, str) and key in {"runtime_spec", "runtimeSpec", "experienceSpecJson", "source_timeline", "timeline"}:
                    try:
                        decoded = json.loads(child)
                    except (ValueError, TypeError):
                        continue
                    before = stats["added"]
                    walk(decoded, f"{path}.{key}")
                    if stats["added"] != before:
                        node[key] = json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))
                else:
                    walk(child, f"{path}.{key}", key)

    walk(repaired)
    return repaired, stats


def json_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def backfill(engine, *, apply=False, expected_database=None):
    if expected_database is not None and engine.url.database != expected_database:
        raise ValueError("Database target does not match the explicitly selected schema")
    metadata = MetaData()
    metadata.reflect(bind=engine)
    report = {"mode": "apply" if apply else "dry-run", "columns": {}, "changed_rows": 0, "added": 0, "pinch_nodes": 0, "invalid": [], "bundles_refreshed": 0}
    with engine.begin() as conn:
        changes = []
        bundles = set()
        for table in metadata.sorted_tables:
            columns = [col for col in table.columns if isinstance(col.type, JSON)]
            keys = list(table.primary_key.columns)
            if not columns or not keys:
                continue
            rows = conn.execute(select(*keys, *columns)).mappings()
            for row in rows:
                values = {}
                for col in columns:
                    repaired, stats = repair_document(row[col.name])
                    report["pinch_nodes"] += stats["pinch_nodes"]
                    report["invalid"].extend(f"{table.name}.{col.name}:{tuple(row[k.name] for k in keys)}:{path}" for path in stats["invalid"])
                    if not stats["added"]:
                        continue
                    values[col.name] = repaired
                    report["added"] += stats["added"]
                    name = f"{table.name}.{col.name}"
                    report["columns"][name] = report["columns"].get(name, 0) + 1
                    if table.name == "run_version_documents" and col.name == "payload":
                        values["content_sha256"], values["size_bytes"] = json_hash(repaired)
                if values:
                    changes.append((table, {key.name: row[key.name] for key in keys}, values))
        report["changed_rows"] = len(changes)
        if report["invalid"]:
            if apply:
                raise ValueError("Invalid existing pinch direction; no changes applied")
            return report
        if apply:
            for table, identity, values in changes:
                condition = and_(*(table.c[name] == value for name, value in identity.items()))
                # Recheck the row under lock: never overwrite concurrent editor changes.
                current = conn.execute(select(table).where(condition).with_for_update()).mappings().one()
                for name in list(values):
                    if name in ("content_sha256", "size_bytes"):
                        continue
                    repaired, stats = repair_document(current[name])
                    if stats["invalid"]:
                        raise ValueError("Concurrent invalid direction; transaction rolled back")
                    values[name] = repaired
                if table.name == "run_version_documents":
                    values["content_sha256"], values["size_bytes"] = json_hash(values["payload"])
                    bundles.add(current["run_version_id"])
                conn.execute(update(table).where(condition).values(**values))
            if bundles:
                documents = metadata.tables["run_version_documents"]
                versions = metadata.tables["run_versions"]
                for version_id in sorted(bundles):
                    parts = conn.execute(select(documents.c.relative_path, documents.c.content_sha256).where(documents.c.run_version_id == version_id).order_by(documents.c.relative_path)).all()
                    digest = hashlib.sha256("\n".join(f"{path}:{sha}" for path, sha in parts).encode()).hexdigest()
                    conn.execute(update(versions).where(versions.c.id == version_id).values(bundle_sha256=digest))
                report["bundles_refreshed"] = len(bundles)
    return report
