from __future__ import annotations

from copy import deepcopy

import httpx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.content_localization import (
    SUPPORTED_LOCALES,
    apply_translation_result,
    extract_source_payload,
    localized_content,
    process_next_content_translation,
    queue_content_translation,
)
from app.locale import (
    locale_for_country,
    parse_accept_language,
    resolve_accept_language,
)
from app.main import app
from app.messages import localized_error
from app.models import (
    ContentLocalization,
    ContentTranslationJob,
    PublishedVideo,
    PublishedVideoSeo,
)
from app.protocol_video import compile_runtime_spec, runtime_spec_version_from_compiled
from app.seo import source_hash as seo_source_hash


def _published(video_id: str = "localized") -> PublishedVideo:
    runtime = compile_runtime_spec(
        item_id=video_id,
        content_mode="single",
        source={"interactions": [{"gesture": "tap", "gate_at_ms": 1000}]},
        video_url=f"/media/{video_id}.mp4",
    )
    runtime["video"][0]["interactions"][0]["success_text"] = "Great move!"
    runtime["video"][0]["interactions"][0]["miss_text"] = "Try the move again."
    return PublishedVideo(
        id=video_id,
        content_type="runtime",
        video_url=f"/media/{video_id}.mp4",
        timeline={"interactions": [{"gesture": "tap", "gate_at_ms": 1000}]},
        runtime_spec=runtime,
        runtime_spec_version=runtime_spec_version_from_compiled(runtime),
        version="1",
        title="Tap the light",
        description="Try it once.",
        content_source="ugc",
        review_status="approved",
    )


def _translated(source: dict, locale: str) -> dict:
    payload = deepcopy(source)
    payload["title"] = f"{locale} title"
    payload["description"] = f"{locale} description"
    for value in payload["interactions"].values():
        value["description"] = f"{locale} action"
        value["success_text"] = f"{locale} success"
        value["miss_text"] = f"{locale} miss"
    return payload


def test_accept_language_weights_and_region_fallbacks() -> None:
    assert parse_accept_language("fr;q=0.5, zh-TW;q=0.9, en;q=0") == [
        "zh-Hant",
        "fr",
    ]
    assert resolve_accept_language("pt-PT,es;q=0.8") == "pt-BR"
    assert resolve_accept_language("de;q=1.5,fr;q=0.8") == "fr"
    assert resolve_accept_language("es;q=oops,ja;q=0.7") == "ja"
    assert resolve_accept_language("it-IT") == "en"
    assert locale_for_country("CN") == "zh-Hans"
    assert locale_for_country("HK") == "zh-Hant"
    assert locale_for_country("MX") == "es"
    assert locale_for_country("AT") == "de"
    assert locale_for_country("US") == "en"


def test_locale_detection_uses_browser_before_trusted_geoip(monkeypatch) -> None:
    monkeypatch.setenv("GEOIP_TRUST_PROXY_HEADERS", "true")
    get_settings.cache_clear()
    calls: list[str] = []

    def country(ip_value: str | None, *, database_path: str) -> str:
        calls.append(str(ip_value))
        return "TW"

    monkeypatch.setattr(
        "app.routers.locale_detection.country_code_for_ip",
        country,
    )
    with TestClient(app) as client:
        browser = client.get(
            "/api/v1/public/locale",
            headers={
                "Accept-Language": "ja-JP",
                "X-Real-IP": "203.0.113.2",
                "X-Forwarded-For": "198.51.100.9",
            },
        )
        geo = client.get(
            "/api/v1/public/locale",
            headers={
                "Accept-Language": "it-IT",
                "X-Real-IP": "203.0.113.3",
                "X-Forwarded-For": "198.51.100.8",
            },
        )
    assert browser.json() == {"locale": "ja", "source": "accept_language"}
    assert geo.json() == {"locale": "zh-Hant", "source": "geoip"}
    assert calls == ["203.0.113.3"]
    get_settings.cache_clear()


def test_locale_middleware_sets_content_language_and_vary() -> None:
    with TestClient(app) as client:
        response = client.get("/health", headers={"Accept-Language": "zh-HK, en;q=0.8"})
    assert response.status_code == 200
    assert response.headers["content-language"] == "zh-Hant"
    assert "Accept-Language" in response.headers["vary"]


def test_batch_result_overlays_only_visible_runtime_text(db) -> None:
    row = _published()
    db.add(row)
    job = queue_content_translation(db, row, source_locale_hint="en")
    db.commit()
    assert job is not None
    source = extract_source_payload(row)
    translations = {
        locale: _translated(source, locale) for locale in job.target_locales
    }
    before = deepcopy(row.runtime_spec)
    apply_translation_result(
        db,
        job,
        {
            "detected_source_locale": "en",
            "translations": translations,
            "model": "test-model",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    )
    title, description, translated_runtime, metadata = localized_content(
        db,
        row,
        "ja",
        enabled=True,
    )
    assert title == "ja title"
    assert description == "ja description"
    assert metadata == {
        "requested_locale": "ja",
        "resolved_locale": "ja",
        "source_locale": "en",
        "status": "ready",
        "machine_translated": True,
    }
    original_interaction = before["video"][0]["interactions"][0]
    translated_interaction = translated_runtime["video"][0]["interactions"][0]
    assert translated_interaction["description"] == "ja action"
    assert translated_interaction["success_text"] == "ja success"
    assert translated_interaction["miss_text"] == "ja miss"
    for field in ("id", "type", "offset_time_ms", "detection", "on_success", "on_miss"):
        assert translated_interaction[field] == original_interaction[field]
    assert row.runtime_spec == before
    assert db.query(ContentLocalization).count() == 8
    source_title, source_description, _source_runtime, source_metadata = (
        localized_content(db, row, "en", enabled=True)
    )
    assert (source_title, source_description) == ("Tap the light", "Try it once.")
    assert source_metadata["status"] == "source"
    assert source_metadata["machine_translated"] is False

    # Replayed callbacks are idempotent and cannot create duplicate locale rows.
    apply_translation_result(
        db,
        job,
        {
            "detected_source_locale": "en",
            "translations": translations,
            "model": "test-model",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    )
    assert db.query(ContentLocalization).count() == 8
    db.refresh(job)
    assert (job.input_tokens, job.output_tokens) == (10, 20)


def test_video_detail_displays_localized_title_description_and_runtime(
    db,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTENT_LOCALIZATION_ENABLED", "true")
    get_settings.cache_clear()
    row = _published("localized-detail")
    db.add(row)
    job = queue_content_translation(db, row)
    db.commit()
    assert job is not None
    source = extract_source_payload(row)
    apply_translation_result(
        db,
        job,
        {
            "detected_source_locale": "en",
            "translations": {"ja": _translated(source, "ja")},
            "model": "test-model",
            "usage": {},
        },
    )

    with TestClient(app) as client:
        response = client.post(
            "/video_detail",
            headers={"Accept-Language": "ja-JP"},
            json={
                "head": {"act": "video_detail", "ver": "1.2"},
                "body": {
                    "video_id": row.id,
                    "supported_experience_spec_versions": ["1.1"],
                },
            },
        )
    assert response.status_code == 200
    item = response.json()["body"]["items"][0]
    assert item["title"] == "ja title"
    assert item["description"] == "ja description"
    assert item["video"][0]["interactions"][0]["description"] == "ja action"
    assert item["localization"]["status"] == "ready"
    assert response.headers["content-language"] == "ja"
    get_settings.cache_clear()


def test_public_seo_derives_interaction_summary_from_localized_runtime(
    db,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTENT_LOCALIZATION_ENABLED", "true")
    get_settings.cache_clear()
    row = _published("localized-seo-summary")
    row.distribution_enabled = True
    row.cdn_ready = True
    db.add(row)
    job = queue_content_translation(db, row)
    db.commit()
    assert job is not None
    source = extract_source_payload(row)
    apply_translation_result(
        db,
        job,
        {
            "detected_source_locale": "en",
            "translations": {"ja": _translated(source, "ja")},
            "model": "test-model",
            "usage": {},
        },
    )
    db.add(
        PublishedVideoSeo(
            video_id=row.id,
            slug="localized-seo-summary-12345",
            page_title=row.title,
            page_description=row.description,
            meta_title=f"{row.title} | Pixopixo",
            meta_description=row.description,
            interaction_types=["tap"],
            interaction_summary="Tap once to continue the scene.",
            thumbnail_url="https://pixopixo.com/posters/localized.jpg",
            status="ready",
            source_hash=seo_source_hash(row),
        )
    )
    db.commit()

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/public/seo/experiences/localized-seo-summary-12345",
            headers={"Accept-Language": "ja"},
        )

    assert response.status_code == 200
    item = response.json()
    assert item["interaction_summary"] == "ja action"
    assert item["interaction_summary_locale"] == "ja"
    get_settings.cache_clear()


def test_source_change_makes_inflight_job_stale(db) -> None:
    row = _published("stale")
    db.add(row)
    job = queue_content_translation(db, row)
    db.commit()
    assert job is not None
    row.title = "A new final title"
    db.commit()
    apply_translation_result(
        db,
        job,
        {
            "detected_source_locale": "en",
            "translations": {},
            "model": "test-model",
            "usage": {},
        },
    )
    assert db.get(ContentTranslationJob, job.id).status == "stale"
    assert db.query(ContentLocalization).count() == 0


def test_source_change_stales_ready_job_and_all_previous_localizations(db) -> None:
    row = _published("ready-then-edited")
    db.add(row)
    old_job = queue_content_translation(db, row)
    db.commit()
    assert old_job is not None
    source = extract_source_payload(row)
    apply_translation_result(
        db,
        old_job,
        {
            "detected_source_locale": "en",
            "translations": {
                locale: _translated(source, locale) for locale in SUPPORTED_LOCALES
            },
            "model": "test-model",
            "usage": {},
        },
    )

    row.title = "The final edited title"
    new_job = queue_content_translation(db, row)
    db.commit()

    assert new_job is not None
    assert new_job.id != old_job.id
    db.refresh(old_job)
    assert old_job.status == "stale"
    assert {
        localization.status
        for localization in db.query(ContentLocalization).all()
    } == {"stale"}


def test_result_validator_preserves_product_names_and_empty_fields(db) -> None:
    row = _published("protected-tokens")
    row.title = "Open Pixo {count}"
    db.add(row)
    job = queue_content_translation(db, row)
    db.commit()
    assert job is not None
    source = extract_source_payload(row)
    invalid = _translated(source, "fr")
    invalid["title"] = "Ouvrez le produit"
    first_interaction = next(iter(invalid["interactions"].values()))
    first_interaction["success_text"] = "Bravo"

    apply_translation_result(
        db,
        job,
        {
            "detected_source_locale": "en",
            "translations": {"fr": invalid},
            "model": "test-model",
            "usage": {},
        },
    )
    db.refresh(job)
    assert job.status == "partial"
    assert db.query(ContentLocalization).count() == 0


def test_translation_request_id_is_bounded_and_requeue_is_unique(db) -> None:
    row = _published("v" * 128)
    db.add(row)
    first = queue_content_translation(db, row)
    db.commit()
    assert first is not None
    assert first.request_id == f"translation:{first.id}"
    assert len(first.request_id) <= 96

    first.status = "failed"
    db.commit()
    second = queue_content_translation(db, row)
    db.commit()
    assert second is not None
    assert second.id != first.id
    assert second.request_id != first.request_id


def test_partial_result_keeps_valid_locales_and_retries_only_missing(db) -> None:
    row = _published("partial")
    db.add(row)
    job = queue_content_translation(db, row)
    db.commit()
    assert job is not None
    job.attempt = 1
    source = extract_source_payload(row)

    apply_translation_result(
        db,
        job,
        {
            "detected_source_locale": "en",
            "translations": {"fr": _translated(source, "fr")},
            "model": "test-model",
            "usage": {"input_tokens": 3, "output_tokens": 5},
        },
    )

    db.refresh(job)
    assert job.status == "partial"
    assert "fr" not in job.target_locales
    assert set(job.target_locales) == set(SUPPORTED_LOCALES) - {"fr"}
    localized = db.query(ContentLocalization).one()
    assert localized.locale == "fr"
    assert localized.source_hash == job.source_hash


def test_invalid_worker_response_retries_three_times_without_blocking_publish(
    db,
) -> None:
    row = _published("worker-retry")
    db.add(row)
    job = queue_content_translation(db, row)
    db.commit()
    assert job is not None

    def invalid_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    settings = get_settings()
    transport = httpx.MockTransport(invalid_json)
    for attempt in range(1, 4):
        assert process_next_content_translation(db, settings, transport=transport)
        db.refresh(job)
        assert job.attempt == attempt
        if attempt < 3:
            assert job.status == "partial"
            job.next_retry_at = None
            db.commit()
        else:
            assert job.status == "failed"
            assert job.completed_at is not None

    # Canonical content remains available while localization falls back.
    title, description, _runtime, metadata = localized_content(
        db,
        row,
        "de",
        enabled=True,
    )
    assert (title, description) == ("Tap the light", "Try it once.")
    assert metadata["status"] == "pending"


def test_html_content_is_never_queued_or_overlaid(db) -> None:
    row = PublishedVideo(
        id="html-localization",
        content_type="html",
        html_url="https://html.test/index.html",
        bridge_version=1,
        version="1",
        title="Creator title",
        description="Creator description",
    )
    db.add(row)
    assert queue_content_translation(db, row) is None
    title, description, runtime, metadata = localized_content(
        db, row, "de", enabled=True
    )
    assert (title, description, runtime) == (
        "Creator title",
        "Creator description",
        None,
    )
    assert metadata["status"] == "unsupported_content_type"


def test_localization_flag_defaults_off() -> None:
    get_settings.cache_clear()
    assert get_settings().content_localization_enabled is False


def test_localized_error_preserves_structured_contract() -> None:
    detail = localized_error(
        "de",
        error_code="INSUFFICIENT_CREDITS",
        message_key="errors.insufficientCredits",
        message_args={"required": 5},
        fallback="You need 5 Credits.",
    )
    assert detail == {
        "code": "INSUFFICIENT_CREDITS",
        "error_code": "INSUFFICIENT_CREDITS",
        "message_key": "errors.insufficientCredits",
        "message_args": {"required": 5},
        "message": "Du benötigst 5 Credits.",
    }


def test_operations_metrics_and_manual_retry_do_not_expose_source_text(db) -> None:
    row = _published("operations")
    db.add(row)
    job = queue_content_translation(db, row)
    db.commit()
    assert job is not None
    job.status = "failed"
    job.last_error = "provider unavailable"
    db.commit()

    with TestClient(app) as client:
        metrics = client.get(
            "/internal/v1/content-localizations/metrics",
            headers={"X-Publish-Key": "test-publish-key"},
        )
        jobs = client.get(
            "/internal/v1/content-localizations/jobs",
            headers={"X-Publish-Key": "test-publish-key"},
        )
        retry = client.post(
            "/internal/v1/content-localizations/retry",
            params={"job_id": job.id},
            headers={"X-Publish-Key": "test-publish-key"},
        )
    assert metrics.status_code == 200
    assert metrics.json()["jobs_by_status"]["failed"] == 1
    assert jobs.status_code == 200
    assert "source_payload" not in jobs.text
    assert "Tap the light" not in jobs.text
    assert retry.json() == {"queued": 1}
    db.refresh(job)
    assert job.status == "queued"
    assert job.attempt == 0
