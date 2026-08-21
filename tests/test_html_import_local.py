from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import html_import_service
from app.html_import_service import (
    HtmlImportError,
    _apply_ai_patch,
    _choose_entry_automatically,
    _html_public_root,
    prepare_local_source,
)


def test_html_public_root_accepts_only_the_immutable_first_party_prefix() -> None:
    settings = SimpleNamespace(
        html_public_base_url=(
            "https://api.pixopixo.cn/ivapp-media/v1/public/html/"
        ),
        oss_root_prefix="ivapp-media/v1",
    )

    assert _html_public_root(settings) == (
        "https://api.pixopixo.cn/ivapp-media/v1/public/html"
    )

    settings.html_public_base_url = "https://api.pixopixo.cn/other/html"
    with pytest.raises(HtmlImportError, match="HTML_PUBLIC_BASE_URL"):
        _html_public_root(settings)


def test_multiple_entries_are_selected_deterministically_and_flagged(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "landing.html").write_text("<html></html>", encoding="utf-8")

    entry, needs_review = _choose_entry_automatically(tmp_path, None)

    assert entry == "demo/index.html"
    assert needs_review is True


def test_ai_patch_is_hash_bound_and_only_changes_derived_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    script = source / "main.js"
    script.write_text("window.addEventListener('custom', onEvent);", encoding="utf-8")
    before = hashlib.sha256(script.read_bytes()).hexdigest()

    audit = _apply_ai_patch(
        source,
        {
            "outcome": "patch",
            "edits": [{
                "path": "main.js",
                "expected_sha256": before,
                "replacements": [{
                    "old": "window.addEventListener('custom', onEvent);",
                    "new": "const off = PixoNative.on('motion', onEvent);",
                }],
            }],
        },
    )

    assert script.read_text(encoding="utf-8") == "const off = PixoNative.on('motion', onEvent);"
    assert audit[0]["before_sha256"] == before
    assert audit[0]["after_sha256"] != before

    with pytest.raises(HtmlImportError, match="hash does not match"):
        _apply_ai_patch(
            source,
            {
                "outcome": "patch",
                "edits": [{
                    "path": "main.js",
                    "expected_sha256": "0" * 64,
                    "replacements": [{"old": "const", "new": "let"}],
                }],
            },
        )


def test_local_pipeline_uses_deterministic_adapter_without_ai_when_qa_passes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import_id = "him_local_pipeline"
    attempt_id = "a" * 32
    source_dir = tmp_path / import_id
    source_dir.mkdir()
    archive = source_dir / "source.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "index.html",
            "<!doctype html><html><head></head><body><video></video>"
            "<script>window.addEventListener('devicemotion', () => {});</script>"
            "</body></html>",
        )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    settings = SimpleNamespace(
        media_storage_mode="oss",
        html_import_spool_root=str(tmp_path),
        html_import_max_zip_bytes=1024 * 1024,
        html_import_max_files=100,
        html_import_max_unpacked_bytes=1024 * 1024,
        html_import_require_playwright=False,
        creator_internal_key="test-key",
        ivadmin_base_url="http://ivadmin.test",
        html_import_progress_timeout_seconds=1.0,
        publish_key="publish-key",
    )
    progress: list[str] = []
    monkeypatch.setattr(
        html_import_service,
        "_notify_progress",
        lambda _settings, **kwargs: progress.append(kwargs["stage"]),
    )
    monkeypatch.setattr(html_import_service, "_ensure_html_creator_pool", lambda _db: None)
    monkeypatch.setattr(html_import_service, "object_key", lambda _settings, *parts: "/".join(parts))
    monkeypatch.setattr(
        html_import_service,
        "public_url",
        lambda _settings, _key: "https://cdn.test/ivapp-media/v1/public/html",
    )

    def fake_prepare(root: Path, stage: Path, **_kwargs):
        stage.mkdir()
        manifest = json.loads((root / "pixo-html.json").read_text(encoding="utf-8"))
        return SimpleNamespace(
            item_id=manifest["item_id"],
            version="b" * 64,
            entry=manifest["entry"],
            html_url=f"https://cdn.test/{manifest['item_id']}/{'b' * 64}/{manifest['entry']}",
            user_id=manifest["user_id"],
            required_capabilities=tuple(manifest["required_capabilities"]),
            compatibility_profile="browser-v1",
            stage_directory=stage,
        )

    monkeypatch.setattr(html_import_service, "prepare_package", fake_prepare)
    monkeypatch.setattr(
        html_import_service,
        "upload_to_oss",
        lambda _package, **_kwargs: {"package_id": "hp_test"},
    )
    monkeypatch.setattr(
        html_import_service,
        "_request_ai_patch",
        lambda *_args, **_kwargs: pytest.fail("AI must not be called when deterministic QA passes"),
    )

    result = prepare_local_source(
        None,  # creator-pool work is mocked; no DB access is needed in this unit test
        settings,
        import_id=import_id,
        attempt_id=attempt_id,
        source_sha256=digest,
        source_bytes=archive.stat().st_size,
        item_id="html_local_test",
        entry=None,
        title="Local test",
        description="",
        user_id="html_creator_001",
        required_capabilities=[],
    )

    assert result["package_id"] == "hp_test"
    assert result["required_capabilities"] == ["motion"]
    assert result["ai"]["used"] is False
    assert result["ai"]["calls"] == 0
    assert progress == [
        "validating_zip",
        "extracting_source",
        "scanning_source",
        "selecting_entry",
        "adapting_compatibility",
        "browser_qa",
        "uploading_preview",
        "finalizing_preview",
    ]
