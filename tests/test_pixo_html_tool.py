from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest

import scripts.pixo_html as pixo_html_module
from scripts.pixo_html import (
    HtmlPackageError,
    prepare_package,
    stable_virtual_author,
    upload_to_oss,
    validate_android_approval,
)
from scripts.seed_html_creators import html_creators

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NATIVE_CLIENT = (
    REPOSITORY_ROOT.parent
    / "Pixo-Android"
    / "pixo-runtime"
    / "src"
    / "pixo-native-client.js"
)
HOST_SDK = REPOSITORY_ROOT / "scripts" / "pixo_html_host_sdk.js"


def _manifest() -> dict:
    return {
        "item_id": "html-neon-001",
        "entry": "index.html",
        "title": "Neon Balance",
        "description": "Tilt and clap",
        "bridge_version": 1,
        "required_capabilities": ["motion", "microphoneLevel", "cameraStream"],
    }


def _write_package(source: Path, *, external_script: str | None = None) -> bytes:
    source.mkdir()
    (source / "pixo-html.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    (source / "assets").mkdir()
    (source / "assets" / "pixel.png").write_bytes(b"png")
    absolute_asset = (source / "assets" / "pixel.png").as_posix()
    (source / "app.js").write_text(
        f'window.appLoaded = true; window.localAsset = "{absolute_asset}?js=1#asset";',
        encoding="utf-8",
    )
    (source / "style.css").write_text(
        "@import '/assets/theme.css'; "
        ".hero { background-image: url('/assets/pixel.png?css=1#asset'); }",
        encoding="utf-8",
    )
    (source / "assets" / "theme.css").write_text(
        ".theme { color: cyan; }",
        encoding="utf-8",
    )
    payload = b"fake-mp4-payload"
    script = external_script or "app.js"
    (source / "index.html").write_text(
        '<!doctype html><html><head><title>Neon</title><link rel="stylesheet" href="style.css">'
        "</head><body>"
        f'<video src="data:video/mp4;base64,{base64.b64encode(payload).decode()}"></video>'
        '<img src="/assets/pixel.png">'
        f'<script src="{script}"></script></body></html>',
        encoding="utf-8",
    )
    (source / "nested").mkdir()
    (source / "nested" / "about.html").write_text(
        "<!doctype html><html><head></head><body>About</body></html>",
        encoding="utf-8",
    )
    return payload


def test_prepare_package_is_immutable_and_injects_every_html_before_business_code(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    payload = _write_package(source)
    original_entry = (source / "index.html").read_text(encoding="utf-8")
    stage = tmp_path / "stage"

    package = prepare_package(
        source,
        stage,
        public_base_url="https://html.test/ivapp-media/v1/public/html",
        native_client_path=NATIVE_CLIENT,
        host_sdk_path=HOST_SDK,
    )

    assert len(package.version) == 64
    assert package.html_url == (
        f"https://html.test/ivapp-media/v1/public/html/html-neon-001/{package.version}/index.html"
    )
    assert (source / "index.html").read_text(encoding="utf-8") == original_entry
    assert package.user_id == stable_virtual_author("html-neon-001")
    assert len(package.extracted_media) == 1
    extracted = stage / package.extracted_media[0]
    assert extracted.read_bytes() == payload

    entry = (stage / "index.html").read_text(encoding="utf-8")
    assert "data:video/" not in entry
    assert f"/{package.version}/{package.extracted_media[0]}" in entry
    assert f"/{package.version}/assets/pixel.png" in entry
    assert entry.index("pixo-html-config.js") < entry.index('src="app.js"')
    assert entry.index("pixo-native-client.js") < entry.index('src="app.js"')
    assert entry.index("pixo-html-host-sdk.js") < entry.index('src="app.js"')

    app_script = (stage / "app.js").read_text(encoding="utf-8")
    assert f"/{package.version}/assets/pixel.png?js=1#asset" in app_script
    stylesheet = (stage / "style.css").read_text(encoding="utf-8")
    assert f"/{package.version}/assets/pixel.png?css=1#asset" in stylesheet
    assert f"/{package.version}/assets/theme.css" in stylesheet

    nested = (stage / "nested" / "about.html").read_text(encoding="utf-8")
    assert 'src="../pixo-host/pixo-html-config.js"' in nested
    config = (stage / "pixo-host" / "pixo-html-config.js").read_text(
        encoding="utf-8",
    )
    assert '"cameraStream"' in config


def test_browser_compatibility_profile_preserves_and_authorizes_inline_business_script(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_package(source)
    business_script = "window.inlineBusinessLoaded = true;"
    entry_path = source / "index.html"
    entry_path.write_text(
        entry_path.read_text(encoding="utf-8").replace(
            "</body>", f"<script>{business_script}</script></body>"
        ),
        encoding="utf-8",
    )

    stage = tmp_path / "stage"
    package = prepare_package(
        source,
        stage,
        public_base_url="https://html.test/ivapp-media/v1/public/html",
        native_client_path=NATIVE_CLIENT,
        host_sdk_path=HOST_SDK,
        browser_compatibility=True,
    )

    assert package.compatibility_profile == "browser-v1"
    config = (stage / "pixo-host" / "pixo-html-config.js").read_text(encoding="utf-8")
    assert '"compatibility_profile":"browser-v1"' in config
    assert '"restart_on_reactivate":true' in config
    entry = (stage / "index.html").read_text(encoding="utf-8")
    expected_hash = base64.b64encode(hashlib.sha256(business_script.encode()).digest()).decode()
    assert f"sha256-{expected_hash}" in entry
    assert "script-src-attr &#x27;unsafe-inline&#x27;" in entry
    assert re.search(r"<script>window\.inlineBusinessLoaded = true;</script>", entry)


def test_prepare_package_percent_encodes_nested_entry_url(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    manifest = _manifest()
    manifest["entry"] = "Hamster Pilot/index.html"
    (source / "pixo-html.json").write_text(json.dumps(manifest), encoding="utf-8")
    nested = source / "Hamster Pilot"
    nested.mkdir()
    (nested / "A.mp4").write_bytes(b"fake-mp4-payload")
    (nested / "index.html").write_text(
        "<!doctype html><html><head></head><body>"
        '<video src="A.mp4"></video></body></html>',
        encoding="utf-8",
    )

    package = prepare_package(
        source,
        tmp_path / "stage",
        public_base_url="https://html.test/ivapp-media/v1/public/html",
        native_client_path=NATIVE_CLIENT,
        host_sdk_path=HOST_SDK,
        browser_compatibility=True,
    )

    assert package.entry == "Hamster Pilot/index.html"
    assert package.html_url.endswith("/Hamster%20Pilot/index.html")
    staged_entry = (package.stage_directory / package.entry).read_text(encoding="utf-8")
    assert 'src="../pixo-host/pixo-html-host-sdk.js"' in staged_entry


def test_prepare_package_rejects_origin_without_a_dedicated_object_prefix(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_package(source)

    with pytest.raises(HtmlPackageError, match="dedicated object prefix"):
        prepare_package(
            source,
            tmp_path / "stage",
            public_base_url="https://html.test",
            native_client_path=NATIVE_CLIENT,
            host_sdk_path=HOST_SDK,
        )

    with pytest.raises(HtmlPackageError, match="ivapp-media/<version>/public/html"):
        prepare_package(
            source,
            tmp_path / "legacy-stage",
            public_base_url="https://html.test/pixo/html",
            native_client_path=NATIVE_CLIENT,
            host_sdk_path=HOST_SDK,
        )

    with pytest.raises(HtmlPackageError, match="ivapp-media/<version>/public/html"):
        prepare_package(
            source,
            tmp_path / "works-stage",
            public_base_url="https://html.test/works/v2/public/html",
            native_client_path=NATIVE_CLIENT,
            host_sdk_path=HOST_SDK,
        )


def test_prepare_package_vendors_external_script_from_approved_asset_origin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    _write_package(source, external_script="https://assets.test/app.js")

    class Headers:
        @staticmethod
        def get_content_type() -> str:
            return "application/javascript"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def geturl() -> str:
            return "https://assets.test/app.js"

        @staticmethod
        def read(_limit: int) -> bytes:
            return b"window.vendorLoaded = true;"

    monkeypatch.setattr(pixo_html_module, "urlopen", lambda *_args, **_kwargs: Response())
    stage = tmp_path / "stage"
    package = prepare_package(
        source,
        stage,
        public_base_url="https://html.test/ivapp-media/v1/public/html",
        native_client_path=NATIVE_CLIENT,
        host_sdk_path=HOST_SDK,
        approved_asset_origins=("https://assets.test",),
    )
    entry = (stage / "index.html").read_text(encoding="utf-8")
    assert "assets.test" not in entry
    assert f"/{package.version}/vendor/" in entry
    assert list((stage / "vendor").glob("*.js"))


def test_prepare_package_recursively_vendors_external_module_dependencies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    _write_package(source, external_script="https://assets.test/app.js")

    class Headers:
        @staticmethod
        def get_content_type() -> str:
            return "application/javascript"

    class Response:
        headers = Headers()

        def __init__(self, url: str, payload: bytes):
            self.url = url
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self) -> str:
            return self.url

        def read(self, _limit: int) -> bytes:
            return self.payload

    def fake_open(request, *_args, **_kwargs):
        url = request.full_url
        if url.endswith("/app.js"):
            return Response(
                url,
                b'import "./nested.js"; window.appLoaded = true;',
            )
        if url.endswith("/nested.js"):
            return Response(url, b"window.nestedLoaded = true;")
        raise AssertionError(url)

    monkeypatch.setattr(pixo_html_module, "urlopen", fake_open)
    stage = tmp_path / "stage"
    package = prepare_package(
        source,
        stage,
        public_base_url="https://html.test/ivapp-media/v1/public/html",
        native_client_path=NATIVE_CLIENT,
        host_sdk_path=HOST_SDK,
        approved_asset_origins=("https://assets.test",),
    )

    vendor_scripts = list((stage / "vendor").glob("*.js"))
    assert len(vendor_scripts) == 2
    combined = "\n".join(path.read_text(encoding="utf-8") for path in vendor_scripts)
    assert "assets.test" not in combined
    assert f"/{package.version}/vendor/" in combined


def test_prepare_package_recursively_vendors_relative_external_css_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    _write_package(source)
    entry_path = source / "index.html"
    entry_path.write_text(
        entry_path.read_text(encoding="utf-8").replace(
            'href="style.css"',
            'href="https://assets.test/theme/main.css"',
        ),
        encoding="utf-8",
    )

    class Headers:
        def __init__(self, content_type: str):
            self.content_type = content_type

        def get_content_type(self) -> str:
            return self.content_type

    class Response:
        def __init__(self, url: str, payload: bytes, content_type: str):
            self.url = url
            self.payload = payload
            self.headers = Headers(content_type)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self) -> str:
            return self.url

        def read(self, _limit: int) -> bytes:
            return self.payload

    def fake_open(request, *_args, **_kwargs):
        url = request.full_url
        if url.endswith("/theme/main.css"):
            return Response(
                url,
                b'@import "./nested.css"; .icon{background:url("../fonts/icon.woff2")}',
                "text/css",
            )
        if url.endswith("/theme/nested.css"):
            return Response(url, b".nested{color:cyan}", "text/css")
        if url.endswith("/fonts/icon.woff2"):
            return Response(url, b"fake-font", "font/woff2")
        raise AssertionError(url)

    monkeypatch.setattr(pixo_html_module, "urlopen", fake_open)
    stage = tmp_path / "stage"
    package = prepare_package(
        source,
        stage,
        public_base_url="https://html.test/ivapp-media/v1/public/html",
        native_client_path=NATIVE_CLIENT,
        host_sdk_path=HOST_SDK,
        approved_asset_origins=("https://assets.test",),
    )

    vendor_css = list((stage / "vendor").glob("*.css"))
    assert len(vendor_css) == 2
    assert len(list((stage / "vendor").glob("*.woff2"))) == 1
    combined = "\n".join(path.read_text(encoding="utf-8") for path in vendor_css)
    assert "assets.test" not in combined
    assert f"/{package.version}/vendor/" in combined


def test_android_approval_is_bound_to_the_immutable_package(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_package(source)
    package = prepare_package(
        source,
        tmp_path / "stage",
        public_base_url="https://html.test/ivapp-media/v1/public/html",
        native_client_path=NATIVE_CLIENT,
        host_sdk_path=HOST_SDK,
    )
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            {
                "item_id": package.item_id,
                "version": package.version,
                "verified": True,
                "device_model": "Pixel 9",
                "tested_at": "2026-08-10T12:00:00Z",
                "checks": {
                    "motion": True,
                    "microphone_level": True,
                    "camera_stream": True,
                    "camera_and_microphone_together": True,
                    "next_releases_resources": True,
                    "background_releases_resources": True,
                },
            }
        ),
        encoding="utf-8",
    )
    assert validate_android_approval(approval, package)["verified"] is True

    approval.write_text(
        approval.read_text(encoding="utf-8").replace(package.version, "0" * 64),
        encoding="utf-8",
    )
    with pytest.raises(HtmlPackageError, match="does not match"):
        validate_android_approval(approval, package)


def test_oss_upload_uses_only_the_immutable_item_version_prefix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    _write_package(source)
    package = prepare_package(
        source,
        tmp_path / "stage",
        public_base_url="https://html.test/ivapp-media/v1/public/html",
        native_client_path=NATIVE_CLIENT,
        host_sdk_path=HOST_SDK,
    )
    declarations: list[dict] = []
    uploaded_paths: list[str] = []
    finalize_timeouts: list[float | None] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = ""

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(
            self,
            url: str,
            *,
            json=None,
            data=None,
            files=None,
            headers=None,
            timeout=None,
        ):
            if url.endswith("/media/upload-sessions"):
                declarations.extend(json["objects"])
                return FakeResponse(
                    201,
                    {
                        "session_id": "mus_test",
                        "uploads": [
                            {
                                "client_ref": item["client_ref"],
                                "url": "https://oss.test",
                                "fields": {"key": f"ivapp-media/v1/public/html/{package.item_id}/{package.version}/{item['relative_path']}"},
                            }
                            for item in json["objects"]
                        ],
                    },
                )
            if url == "https://oss.test":
                uploaded_paths.append(next(iter(files.values()))[0])
                return FakeResponse(204)
            finalize_timeouts.append(timeout)
            return FakeResponse(
                200,
                {
                    "package_id": "hp_test",
                    "objects": [
                        {
                            "object_key": f"ivapp-media/v1/public/html/{package.item_id}/{package.version}/{item['relative_path']}"
                        }
                        for item in declarations
                    ],
                },
            )

    monkeypatch.setattr(pixo_html_module.httpx, "Client", FakeClient)
    result = upload_to_oss(
        package,
        backend_url="https://api.test",
        publish_key="publish-key",
    )

    prefix = f"ivapp-media/v1/public/html/{package.item_id}/{package.version}/"
    assert result["package_id"] == "hp_test"
    assert uploaded_paths
    assert finalize_timeouts == [1800.0]
    assert all(item["object_key"].startswith(prefix) for item in result["objects"])


def test_virtual_author_assignment_and_catalog_are_stable() -> None:
    creators = html_creators()
    assert [creator.user_id for creator in creators] == [
        f"html_creator_{index:03d}" for index in range(1, 101)
    ]
    assert len({creator.nickname for creator in creators}) == 100
    assert all(creator.payload()["provider"] == "content_pool" for creator in creators)
    assert all(creator.payload()["avatar_url"] == "" for creator in creators)

    item_id = "html-neon-001"
    expected_slot = int.from_bytes(
        hashlib.sha256(item_id.encode("utf-8")).digest(),
        "big",
    ) % 100
    assert stable_virtual_author(item_id) == f"html_creator_{expected_slot + 1:03d}"
