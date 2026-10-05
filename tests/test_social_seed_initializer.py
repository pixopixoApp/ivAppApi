from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from app.models import User, UserToken
from app.oss_storage import OssObjectMetadata, OssObjectNotFoundError
from scripts import init_social_seed_accounts as initializer
from scripts.init_social_seed_accounts import account_specs


def test_social_seed_account_specs_are_deterministic_and_unique() -> None:
    first = account_specs("prelaunch-v1", 100)
    second = account_specs("prelaunch-v1", 100)

    assert first == second
    assert len(first) == 100
    assert len({item["user_id"] for item in first}) == 100
    assert len({item["nickname"] for item in first}) == 100
    assert len({item["avatar_seed"] for item in first}) == 100
    assert len({item["avatar_style"] for item in first}) == len(initializer.AVATAR_STYLES)
    assert all(not item["nickname"].isdigit() for item in first)
    assert not any(item["nickname"].startswith("Amber") for item in first)
    assert all(
        re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,31}", item["nickname"])
        for item in first
    )


def test_social_seed_provision_is_idempotent_from_five_to_one_hundred(
    db, monkeypatch, tmp_path
) -> None:
    del db
    objects: dict[str, tuple[bytes, dict[str, str]]] = {}
    downloads: list[tuple[str, str]] = []
    settings = SimpleNamespace(media_storage_mode="oss")
    monkeypatch.setattr(initializer, "get_settings", lambda: settings)
    monkeypatch.setattr(
        initializer,
        "object_key",
        lambda _settings, *parts: "/".join(("ivapp-media/v1", *parts)),
    )
    monkeypatch.setattr(
        initializer,
        "public_url",
        lambda _settings, key: f"https://media.example/{key}",
    )

    def download(style: str, seed: str) -> bytes:
        downloads.append((style, seed))
        return b"\x89PNG\r\n\x1a\n" + style.encode() + seed.encode()

    def upload(_settings, *, key, payload, extra_headers, **_kwargs):
        objects[key] = (payload, dict(extra_headers))
        return f"https://media.example/{key}"

    def head(_settings, *, key):
        if key not in objects:
            raise OssObjectNotFoundError(key)
        payload, headers = objects[key]
        return OssObjectMetadata(
            size_bytes=len(payload),
            content_type="image/png",
            etag="test",
            headers=headers,
        )

    monkeypatch.setattr(initializer, "_download_avatar", download)
    monkeypatch.setattr(initializer, "upload_bytes", upload)
    monkeypatch.setattr(initializer, "head_object", head)
    manifest = tmp_path / "manifest.json"

    initializer.provision(batch_id="prelaunch-v1", count=5, manifest_path=manifest)
    initializer.provision(batch_id="prelaunch-v1", count=5, manifest_path=manifest)
    initializer.provision(batch_id="prelaunch-v1", count=100, manifest_path=manifest)
    initializer.provision(batch_id="prelaunch-v1", count=100, manifest_path=manifest)

    from app.db import SessionLocal

    with SessionLocal() as session:
        rows = session.query(User).filter_by(
            internal_purpose="social_seed", internal_batch="prelaunch-v1"
        ).all()
        assert len(rows) == 100
        assert len({row.nickname for row in rows}) == 100
        assert session.query(UserToken).filter(
            UserToken.user_id.in_([row.user_id for row in rows])
        ).count() == 0
    assert len(downloads) == 100
    assert len(objects) == 100
    saved = json.loads(manifest.read_text())
    assert len(saved["accounts"]) == 100
    assert len({item["dicebear_style"] for item in saved["accounts"]}) == 12
    for item in saved["accounts"]:
        payload, _headers = objects[item["oss_key"]]
        assert item["sha256"] == hashlib.sha256(payload).hexdigest()
