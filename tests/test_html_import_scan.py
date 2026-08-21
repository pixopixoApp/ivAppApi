from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

from app.html_import_service import _safe_extract, _scan


def test_scan_maps_browser_apis_to_compatible_native_capabilities(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        """
        <!doctype html><html><head></head><body><video></video><script>
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: false }, video: false
        });
        const context = new AudioContext();
        context.createMediaStreamSource(stream).connect(context.createAnalyser());
        await DeviceMotionEvent.requestPermission();
        window.addEventListener("devicemotion", onMotion);
        navigator.vibrate(100);
        </script></body></html>
        """,
        encoding="utf-8",
    )

    result = _scan(tmp_path)

    assert result["suggested_capabilities"] == [
        "motion",
        "microphoneLevel",
        "haptics",
    ]
    assert result["compatibility_profile"] == "browser-v1"
    assert result["unsupported_features"] == []


def test_scan_distinguishes_camera_and_reports_unsupported_audio_features(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.html").write_text(
        """
        <html><head></head><body><video></video><script>
        navigator.mediaDevices.getUserMedia({video:{facingMode:"user"},audio:false});
        navigator.mediaDevices.getUserMedia({video:true,audio:true});
        const recorder = new MediaRecorder(stream);
        analyser.getByteFrequencyData(values);
        </script></body></html>
        """,
        encoding="utf-8",
    )

    result = _scan(tmp_path)

    assert result["suggested_capabilities"] == ["microphoneLevel", "cameraStream"]
    assert result["unsupported_features"] == [
        "combined_camera_and_microphone_capture",
        "frequency_audio_analysis",
        "microphone_recording",
    ]


def test_scan_conservatively_guards_aliased_get_user_media(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        """
        <html><body><video></video><script>
        const capture = navigator.mediaDevices["getUserMedia"];
        capture(buildConstraints());
        </script></body></html>
        """,
        encoding="utf-8",
    )

    result = _scan(tmp_path)

    assert result["suggested_capabilities"] == ["microphoneLevel", "cameraStream"]
    assert result["compatibility_warnings"] == ["dynamic_get_user_media_constraints"]


def test_safe_extract_ignores_macos_metadata(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Hamster Pilot/index.html", "<video src='A.mp4'></video>")
        output.writestr("Hamster Pilot/A.mp4", b"video")
        output.writestr("__MACOSX/Hamster Pilot/._index.html", b"appledouble")
        output.writestr("Hamster Pilot/.DS_Store", b"finder")
    destination = tmp_path / "source"
    destination.mkdir()
    settings = SimpleNamespace(
        html_import_max_zip_bytes=1024 * 1024,
        html_import_max_files=100,
        html_import_max_unpacked_bytes=1024 * 1024,
    )

    files = _safe_extract(archive, destination, settings)

    assert files == ["Hamster Pilot/A.mp4", "Hamster Pilot/index.html"]
    assert not (destination / "__MACOSX").exists()
    assert not (destination / "Hamster Pilot" / ".DS_Store").exists()
