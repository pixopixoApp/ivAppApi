from __future__ import annotations

import hashlib
import json
import struct
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.media_cache import upload_staging_path
from app.models import (
    CreatorAccessGrant,
    CreatorCreation,
    CreatorUpload,
    User,
    UserToken,
)
from app.video_probe import probe_video


@pytest.fixture(scope="module")
def rotated_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("real-video-prepare")
    source, rotated = root / "source.mp4", root / "rotated.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=48000",
            "-t",
            "35",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p10le",
            "-g",
            "10",
            "-bf",
            "2",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(source),
        ],
        check=True,
    )
    # Set the video's standard tkhd display matrix without relying on ffmpeg's
    # deprecated rotate metadata option (which some builds silently ignore).
    data = bytearray(source.read_bytes())

    def boxes(start, end):
        while start + 8 <= end:
            size = int.from_bytes(data[start : start + 4], "big")
            kind = bytes(data[start + 4 : start + 8])
            if size < 8:
                break
            yield start, size, kind
            start += size

    moov = next((p, size) for p, size, kind in boxes(0, len(data)) if kind == b"moov")
    trak = next(
        (p, size) for p, size, kind in boxes(moov[0] + 8, sum(moov)) if kind == b"trak"
    )
    tkhd = next(p for p, size, kind in boxes(trak[0] + 8, sum(trak)) if kind == b"tkhd")
    matrix = tkhd + (60 if data[tkhd + 8] == 1 else 48)
    data[matrix : matrix + 36] = struct.pack(
        ">9i", 0, -65536, 0, 65536, 0, 0, 0, 0, 1 << 30
    )
    rotated.write_bytes(data)
    return rotated


@pytest.fixture
def web_client(db, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CACHE_ENABLED", "true")
    monkeypatch.setenv("MEDIA_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_CACHE_MIN_FREE_BYTES", "1048576")
    monkeypatch.setenv("CREATOR_LOCAL_UPLOAD_ENABLED", "true")
    get_settings.cache_clear()
    now = datetime.now(timezone.utc)
    for user_id in ("prepare-owner", "prepare-other"):
        db.add(
            User(user_id=user_id, provider="email", subject=f"{user_id}@example.com")
        )
        db.add(
            UserToken(
                token=user_id,
                user_id=user_id,
                created_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        db.add(CreatorAccessGrant(user_id=user_id, source="test"))
    db.commit()
    with TestClient(app) as client:
        client.cookies.set("pixo_web_session", "prepare-owner")
        client.get("/api/v1/web/config")
        yield client


def headers(client):
    return {"X-Pixo-CSRF": client.cookies.get("pixo_web_csrf")}


def upload(client, source, *, profile="first-30s-v1"):
    data = source.read_bytes()
    payload = {
        "filename": "source.mp4",
        "content_type": "video/mp4",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "supported_transports": ["local-resumable-v1"],
    }
    if profile:
        payload["preparation_profile"] = profile
    initialized = client.post(
        "/api/v1/creator/uploads/init", headers=headers(client), json=payload
    )
    assert initialized.status_code == 201, initialized.text
    session = initialized.json()["session_id"]
    r = client.patch(
        f"/api/v1/creator/uploads/{session}/source",
        headers={
            **headers(client),
            "Upload-Offset": "0",
            "Content-Type": "application/offset+octet-stream",
        },
        content=data,
    )
    assert r.status_code == 204, r.text
    return session


def finalize(client, session):
    return client.post(
        f"/api/v1/creator/uploads/{session}/finalize",
        headers=headers(client),
        json={"manifest_hash": ""},
    )


def test_real_trim_keeps_audio_orientation_and_checksum_and_is_idempotent(
    web_client, rotated_source, db, tmp_path
):
    session = upload(web_client, rotated_source)
    response = finalize(web_client, session)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["duration_ms"] <= 30_000
    assert result["original_duration_ms"] == 35_000
    assert result["was_trimmed"] is True
    media = web_client.get(result["prepared_source_url"])
    assert media.status_code == 200 and "no-store" in media.headers["cache-control"]
    output = tmp_path / "prepared.mp4"
    output.write_bytes(media.content)
    metadata = probe_video(output)
    assert metadata.duration_ms == 30_000
    assert (metadata.width, metadata.height) == (90, 160)
    streams = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]
        )
    )["streams"]
    assert {s["codec_type"] for s in streams} == {"video", "audio"}
    assert (
        next(s for s in streams if s["codec_type"] == "video")["pix_fmt"] == "yuv420p"
    )
    row = db.get(CreatorUpload, result["upload_id"])
    assert row.source_sha256 == hashlib.sha256(media.content).hexdigest()
    assert row.size_bytes == len(media.content) == result["size_bytes"]
    assert row.source_sha256 != hashlib.sha256(rotated_source.read_bytes()).hexdigest()
    assert not upload_staging_path(get_settings(), session).exists()
    assert finalize(web_client, session).json() == result
    assert (
        db.query(CreatorUpload).count() == 1 and db.query(CreatorCreation).count() == 0
    )
    web_client.cookies.set("pixo_web_session", "prepare-other")
    assert web_client.get(result["prepared_source_url"]).status_code == 404
    assert finalize(web_client, session).status_code == 404


def test_existing_android_upload_still_rejects_long_video(web_client, rotated_source):
    session = upload(web_client, rotated_source, profile=None)
    assert finalize(web_client, session).status_code == 400
    assert upload_staging_path(get_settings(), session).exists()


def test_failed_preparation_retains_source_and_retry_succeeds(
    web_client, rotated_source, monkeypatch
):
    from app.creator_video_prepare import VideoPreparationError
    from app.routers import media_storage

    original = media_storage.prepared_creator_video

    def fail(*_args):
        raise VideoPreparationError("Could not prepare this source")

    session = upload(web_client, rotated_source)
    monkeypatch.setattr(media_storage, "prepared_creator_video", fail)
    assert finalize(web_client, session).status_code == 400
    assert upload_staging_path(get_settings(), session).exists()
    monkeypatch.setattr(media_storage, "prepared_creator_video", original)
    assert finalize(web_client, session).status_code == 201


def test_preparation_is_negotiated_and_web_only(web_client):
    config = web_client.get("/api/v1/web/config").json()["creator"]
    assert (
        config["preparation_profile"] == "first-30s-v1"
        and config["max_source_bytes"] > config["max_bytes"]
    )
    payload = {
        "filename": "large.mp4",
        "size_bytes": config["max_bytes"] + 1,
        "supported_transports": ["local-resumable-v1"],
    }
    assert (
        web_client.post(
            "/api/v1/creator/uploads/init", headers=headers(web_client), json=payload
        ).status_code
        == 400
    )
    payload["preparation_profile"] = "first-30s-v1"
    assert (
        web_client.post(
            "/api/v1/creator/uploads/init", headers=headers(web_client), json=payload
        ).status_code
        == 201
    )
    payload["size_bytes"] = config["max_source_bytes"] + 1
    assert (
        web_client.post(
            "/api/v1/creator/uploads/init", headers=headers(web_client), json=payload
        ).status_code
        == 400
    )
    payload["size_bytes"] = 1024
    web_client.cookies.clear()
    assert (
        web_client.post(
            "/api/v1/creator/uploads/init",
            headers={"Authorization": "Bearer prepare-owner"},
            json=payload,
        ).status_code
        == 400
    )


def test_disabled_transport_does_not_advertise_preparation(web_client, monkeypatch):
    monkeypatch.setenv("CREATOR_LOCAL_UPLOAD_ENABLED", "false")
    get_settings.cache_clear()
    config = web_client.get("/api/v1/web/config").json()["creator"]
    assert config["preparation_profile"] is None


def test_short_preparation_profile_preserves_original_bytes(
    web_client, rotated_source, tmp_path
):
    short = tmp_path / "short.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(rotated_source),
            "-t",
            "2",
            "-c",
            "copy",
            str(short),
        ],
        check=True,
    )
    session = upload(web_client, short)
    result = finalize(web_client, session)
    assert result.status_code == 201, result.text
    result = result.json()
    assert result["was_trimmed"] is False
    assert result["original_duration_ms"] <= 3000
    assert web_client.get(result["prepared_source_url"]).content == short.read_bytes()
