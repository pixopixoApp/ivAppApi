"""Local-only ivadmin contract stub for the Web creator development stack.

It exercises ivapp's real normalization, coordinator, polling and Runtime
compilation path without requiring the operations database or model gateway.
Production never imports or starts this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _identifier(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MockCreatorStore:
    def __init__(self, path: Path, *, stage_seconds: float = 1.5):
        self.path = path
        self.stage_seconds = max(0.2, float(stage_seconds))
        self.lock = threading.Lock()
        self.state: dict[str, dict[str, dict[str, Any]]] = {
            "normalizations": {},
            "jobs": {},
        }
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        for key in ("normalizations", "jobs"):
            if isinstance(payload.get(key), dict):
                self.state[key] = payload[key]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix(".tmp")
        staging.write_text(json.dumps(self.state, separators=(",", ":")), encoding="utf-8")
        staging.replace(self.path)

    def normalization(self, body: dict[str, Any]) -> dict[str, Any]:
        request_id = str(body.get("request_id") or "").strip()
        digest = str(body.get("source_sha256") or "").strip().lower()
        size = int(body.get("source_size_bytes") or 0)
        if not request_id or len(digest) != 64 or size <= 0:
            raise ValueError("invalid normalization request")
        job_id = _identifier("mnj", request_id)
        with self.lock:
            row = self.state["normalizations"].setdefault(
                job_id,
                {
                    "job_id": job_id,
                    "request_id": request_id,
                    "owner_type": str(body.get("owner_type") or "creator_upload"),
                    "owner_id": str(body.get("owner_id") or ""),
                    "profile": "mobile-v1",
                    "source_sha256": digest,
                    "source_size_bytes": size,
                    "created_at": _iso_now(),
                },
            )
            self._save()
            return self.normalization_payload(row)

    def normalization_by_id(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.state["normalizations"].get(job_id)
            return self.normalization_payload(row) if row else None

    @staticmethod
    def normalization_payload(row: dict[str, Any]) -> dict[str, Any]:
        digest = str(row["source_sha256"])
        size = int(row["source_size_bytes"])
        uri = f"local-cache://sha256/{digest}"
        return {
            **row,
            "status": "ready",
            "progress_stage": "ready",
            "progress_percent": 100,
            "source_local_uri": uri,
            "source_media_object_id": None,
            "playable_sha256": digest,
            "playable_size_bytes": size,
            "playable_local_uri": uri,
            "playable_media_object_id": None,
            "duration_ms": 10_000,
            "width": 1080,
            "height": 1920,
            "action": "COPY",
            "reasons": ["local_creator_contract_stub"],
            "backup_status": "pending",
            "error_code": None,
            "error_message": "",
            "updated_at": _iso_now(),
        }

    def create_job(
        self,
        body: dict[str, Any],
        *,
        kind: str = "initial",
        run_id: str = "",
    ) -> dict[str, Any]:
        request_id = str(body.get("request_id") or "").strip()
        creation_id = str(body.get("creation_id") or "").strip()
        if not request_id or not creation_id:
            raise ValueError("invalid creator job request")
        job_id = _identifier("mcj", request_id)
        with self.lock:
            row = self.state["jobs"].setdefault(
                job_id,
                {
                    "job_id": job_id,
                    "request_id": request_id,
                    "creation_id": creation_id,
                    "kind": kind,
                    "run_id": run_id or _identifier("run", creation_id),
                    "version": "0.0.1",
                    "brief": str(body.get("brief") or ""),
                    "created_epoch": time.time(),
                    "created_at": _iso_now(),
                    "cancelled": False,
                },
            )
            self._save()
            return self.job_payload(row)

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.state["jobs"].get(job_id)
            return self.job_payload(row) if row else None

    def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.state["jobs"].get(job_id)
            if row is None:
                return None
            row["cancelled"] = True
            self._save()
            return self.job_payload(row)

    def job_payload(self, row: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        if row.get("cancelled"):
            status, stage, percent = "cancelled", "cancelled", 0
        else:
            elapsed = max(0.0, (time.time() if now is None else now) - float(row["created_epoch"]))
            index = int(elapsed // self.stage_seconds)
            stages = [
                ("running", "validate_video", 12),
                ("running", "sample_frames", 36),
                ("running", "find_playable_moments", 68),
                ("running", "compile_preview", 88),
                ("ready", "ready", 100),
            ]
            status, stage, percent = stages[min(index, len(stages) - 1)]
        timeline = None
        if status == "ready":
            timeline = {
                "interactions": [
                    {
                        "gesture": "tap",
                        "gate_at_ms": 1000,
                        "hint": "Tap to continue",
                    }
                ]
            }
        return {
            "job_id": row["job_id"],
            "request_id": row["request_id"],
            "creation_id": row["creation_id"],
            "kind": row["kind"],
            "status": status,
            "progress_stage": stage,
            "progress_percent": percent,
            "cancel_requested": bool(row.get("cancelled")),
            "run_id": row["run_id"],
            "version": row["version"],
            "timeline": timeline,
            "error_code": None,
            "error_message": None,
            "created_at": row["created_at"],
            "updated_at": _iso_now(),
        }


class MockCreatorHandler(BaseHTTPRequestHandler):
    server: MockCreatorServer

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[mock-creator] {format_string % args}", flush=True)

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise TypeError("JSON object required")
        return payload

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("X-Creator-Internal-Key") == self.server.internal_key:
            return True
        self._send(401, {"detail": "Invalid creator internal key"})
        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self._send(200, {"ok": True, "service": "local-creator-contract-stub"})
            return
        if not self._authorized():
            return
        normalization_prefix = "/internal/v1/mobile-creator/normalizations/"
        job_prefix = "/internal/v1/mobile-creator/jobs/"
        if path.startswith(normalization_prefix):
            payload = self.server.store.normalization_by_id(path[len(normalization_prefix) :])
        elif path.startswith(job_prefix):
            payload = self.server.store.job(path[len(job_prefix) :])
        else:
            payload = None
        self._send(200, payload) if payload else self._send(404, {"detail": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._authorized():
            return
        try:
            if path == "/internal/v1/mobile-creator/normalizations":
                payload = self.server.store.normalization(self._json_body())
            elif path == "/internal/v1/mobile-creator/jobs/from-normalization":
                payload = self.server.store.create_job(self._json_body())
            elif path.startswith("/internal/v1/mobile-creator/runs/") and path.endswith("/versions"):
                run_id = path.split("/")[5]
                payload = self.server.store.create_job(
                    self._json_body(),
                    kind="version",
                    run_id=run_id,
                )
            elif path.startswith("/internal/v1/mobile-creator/jobs/") and path.endswith("/cancel"):
                job_id = path.removesuffix("/cancel").rsplit("/", 1)[-1]
                payload = self.server.store.cancel_job(job_id)
                if payload is None:
                    self._send(404, {"detail": "Not found"})
                    return
            else:
                self._send(404, {"detail": "Not found"})
                return
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"detail": str(exc)})
            return
        self._send(202, payload)


class MockCreatorServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        *,
        store: MockCreatorStore,
        internal_key: str,
    ):
        super().__init__(address, MockCreatorHandler)
        self.store = store
        self.internal_key = internal_key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    key = os.environ.get("CREATOR_INTERNAL_KEY", "local-web-creator").strip()
    stage_seconds = float(os.environ.get("PIXO_MOCK_CREATOR_STAGE_SECONDS", "1.5"))
    server = MockCreatorServer(
        (args.host, args.port),
        store=MockCreatorStore(args.state, stage_seconds=stage_seconds),
        internal_key=key,
    )
    print(f"[mock-creator] listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
