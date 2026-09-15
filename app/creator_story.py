"""Creator ABC stories: durable plans, independent endings and deterministic compilation."""
from __future__ import annotations

import hashlib
import json
import secrets
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.cdn_cache import enqueue_private_media_prefetch
from app.config import get_settings
from app.creator_interaction_presets import (
    apply_preset_fields,
    resolve_interaction_preset,
)
from app.creator_manual_edits import SUSTAINED
from app.credits import InsufficientCredits, grant_welcome_credit, reserve
from app.models import (
    CreatorCreation,
    CreatorSourceGeneration,
    CreatorUpload,
    CreatorVersion,
    MediaObject,
)
from app.protocol_video import (
    compile_runtime_spec,
    runtime_spec_version_from_compiled,
)

ROLES = ("B", "C")
ACTIVE = {"queued", "running"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def editable(creation: CreatorCreation) -> None:
    if creation.status in {"published", "pending_review", "abandoned", "deleted"}:
        raise HTTPException(409, "This creation can no longer be edited")


def source_upload(db: Session, creation: CreatorCreation) -> CreatorUpload:
    upload = db.get(CreatorUpload, creation.upload_id) if creation.upload_id else None
    if upload is None or upload.user_id != creation.user_id or upload.normalization_status != "ready":
        raise HTTPException(409, "The source video is not ready")
    if creation.source_mode == "prompt":
        generation = db.get(CreatorSourceGeneration, creation.source_generation_id)
        if generation is None or generation.accepted_at is None:
            raise HTTPException(409, "Confirm the source video before choosing Story")
    return upload


def video_input(plan: dict, role: str) -> dict:
    return {"source_upload_id": plan["source_upload_id"], "split_ms": plan["split_ms"],
            "source_tail_owner": plan["source_tail_owner"],
            "prompt": plan["success_prompt" if role == "B" else "miss_prompt"]}


def save_plan(db: Session, creation: CreatorCreation, payload: dict) -> None:
    editable(creation)
    upload = source_upload(db, creation)
    if db.query(CreatorVersion).filter(CreatorVersion.creation_id == creation.id,
                                      CreatorVersion.status.in_(ACTIVE)).first():
        raise HTTPException(409, "Wait for the current interaction analysis to finish")
    try:
        preset = resolve_interaction_preset(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if preset.type in SUSTAINED or not preset.story_enabled:
        raise HTTPException(422, "Choose a discrete Story interaction; continuous types are Auto-only")
    old = dict(creation.story_plan or {})
    owner = payload.get("source_tail_owner")
    split = payload.get("split_ms", 0) if owner else 0
    if owner and (split < 1000 or upload.duration_ms - split < 1000):
        raise HTTPException(422, "Keep at least one second on each side of the source cut")
    values = {"interaction_type": preset.type, "interaction_preset_id": preset.id,
              "success_prompt": payload["success_prompt"].strip(),
              "miss_prompt": payload["miss_prompt"].strip(), "source_tail_owner": owner,
              "split_ms": split, "source_upload_id": upload.id}
    if preset.pinch_direction is not None:
        values["pinch_direction"] = preset.pinch_direction
    if preset.rotation_direction is not None:
        values["rotation_direction"] = preset.rotation_direction
    if preset.vision_target is not None:
        values["vision_target"] = preset.vision_target
    if old and all(old.get(key) == value for key, value in values.items()):
        creation.experience_mode = "story"
        return
    if payload["revision"] != old.get("revision", 0):
        raise HTTPException(409, "The Story plan changed; reload before saving")
    plan = {**values, "revision": old.get("revision", 0) + 1, "status": "draft", "generation_ids": {},
            "assets": {}, "requests": deepcopy(old.get("requests", {})), "error_message": ""}
    same_cut = all(old.get(k) == values[k] for k in ("source_upload_id", "split_ms", "source_tail_owner"))
    if same_cut:
        plan["assets"] = deepcopy(old.get("assets", {}))
    for role in ROLES:
        if old and video_input(old, role) == video_input(plan, role) and role in old.get("generation_ids", {}):
            plan["generation_ids"][role] = old["generation_ids"][role]
    if not owner:
        plan["assets"]["A"] = upload.id
    creation.story_plan = plan
    creation.experience_mode = "story"
    creation.status = "source_ready"
    creation.progress_stage = "story_plan"
    creation.updated_at = now()


def ending(db: Session, plan: dict, role: str) -> CreatorSourceGeneration | None:
    identifier = plan.get("generation_ids", {}).get(role)
    return db.get(CreatorSourceGeneration, identifier) if identifier else None


def start_generation(db: Session, creation: CreatorCreation, payload: dict) -> None:
    editable(creation)
    settings = get_settings()
    if not (settings.creator_branch_story_enabled and settings.creator_text_to_video_enabled):
        raise HTTPException(503, "Story video generation is not enabled in this environment")
    upload = source_upload(db, creation)
    plan = deepcopy(creation.story_plan or {})
    if not plan or plan["revision"] != payload["revision"]:
        raise HTTPException(409, "The Story plan changed; reload before generating")
    if len(set(payload["targets"])) != len(payload["targets"]):
        raise HTTPException(422, "Duplicate ending targets")
    request_id = payload["request_id"]
    signature = digest({k: payload[k] for k in ("revision", "targets", "operation")})
    previous = plan.get("requests", {}).get(request_id)
    if previous:
        if previous != signature:
            raise HTTPException(409, "Request ID was reused with different input")
        return
    from app.creator_drafts import require_other_creations_idle
    require_other_creations_idle(db, creation.user_id, creation.id)
    selected = []
    for role in payload["targets"]:
        if role == plan["source_tail_owner"]:
            continue
        prompt = plan["success_prompt" if role == "B" else "miss_prompt"]
        if not prompt:
            raise HTTPException(422, f"Describe ending {role} before generating it")
        prior = ending(db, plan, role)
        if prior and prior.status in ACTIVE:
            raise HTTPException(409, f"Ending {role} is already generating")
        if prior and prior.status == "ready" and payload["operation"] != "regenerate":
            continue
        if payload["operation"] == "retry" and (
            prior is None or prior.status not in {"failed", "cancelled", "expired"}
        ):
            raise HTTPException(409, f"Ending {role} does not need a retry")
        selected.append(role)
    grant_welcome_credit(db, creation.user_id)
    db.flush()
    number = int(db.query(func.max(CreatorSourceGeneration.attempt)).filter_by(creation_id=creation.id).scalar() or 0)
    for role in selected:
        number += 1
        identifier = f"csg_{secrets.token_urlsafe(18)}"
        cost = max(1, settings.creator_video_duration_seconds)
        try:
            reserve(db, user_id=creation.user_id, reference_id=f"source:{identifier}",
                    amount=cost, purpose=f"Story ending {role} · {cost} seconds")
        except InsufficientCredits as exc:
            raise HTTPException(402, {"code": "INSUFFICIENT_CREDITS",
                                     "message": f"You need {len(selected) * cost} Credits for these endings."}) from exc
        prior = ending(db, plan, role)
        snapshot = {**video_input(plan, role), "revision": plan["revision"],
                    "first_frame_source": upload.playable_local_uri,
                    "first_frame_at_ms": plan["split_ms"] or upload.duration_ms}
        if prior and prior.upload_id and prior.status == "ready":
            snapshot["previous_upload_id"] = prior.upload_id
        generation = CreatorSourceGeneration(
            id=identifier, creation_id=creation.id, user_id=creation.user_id, attempt=number,
            request_id=f"story:{request_id}:{role}", original_prompt=snapshot["prompt"], generation_kind=role,
            input_json=snapshot, status="queued", progress_stage="queued", progress_percent=0,
            quota_date=now().date().isoformat(), quota_state="reserved", next_poll_at=now(),
            expires_at=now() + timedelta(days=settings.creator_video_draft_ttl_days),
        )
        db.add(generation)
        plan["generation_ids"][role] = identifier
    plan.setdefault("requests", {})[request_id] = signature
    plan["status"] = "generating"
    plan["error_message"] = ""
    creation.story_plan = plan
    creation.experience_mode = "story"
    creation.status = "running"
    creation.progress_stage = "generating_endings"
    creation.updated_at = now()
    db.flush()
    sync_story(db, creation)


def story_out(db: Session, creation: CreatorCreation) -> dict | None:
    plan = creation.story_plan
    if not plan:
        return None
    clips = {}
    for role in ("A", "B", "C"):
        generation = ending(db, plan, role) if role in ROLES and role != plan["source_tail_owner"] else None
        upload_id = generation.upload_id if generation else plan.get("assets", {}).get(role)
        upload = db.get(CreatorUpload, upload_id) if upload_id else None
        status = generation.status if generation else (
            upload.normalization_status if upload else "pending"
        )
        previous_id = (generation.input_json or {}).get("previous_upload_id") if generation else None
        clips[role] = {"status": status, "generation_id": generation.id if generation else None,
                       "source_kind": "generated" if generation else "source_clip",
                       "progress_stage": generation.progress_stage if generation else status,
                       "progress_percent": generation.progress_percent if generation else 100 if upload else 0,
                       "preview_url": f"/api/v1/creator/previews/{upload.id}" if upload and upload.normalization_status == "ready" else None,
                       "previous_preview_url": f"/api/v1/creator/previews/{previous_id}" if previous_id else None,
                       "duration_ms": upload.duration_ms if upload else (
                           get_settings().creator_video_duration_seconds * 1000
                           if generation else 0
                       ),
                       "error_message": generation.error_message if generation else "",
                       "updated_at": generation.updated_at.isoformat() if generation and generation.updated_at else None}
    return {k: deepcopy(v) for k, v in plan.items() if k not in {"assets", "requests", "generation_ids"}} | {"clips": clips}


def register_cut(db: Session, creation: CreatorCreation, metadata: dict, role: str) -> str:
    sha = str(metadata.get("source_sha256") or "")
    size = int(metadata.get("source_size_bytes") or 0)
    duration = int(metadata.get("source_duration_ms") or 0)
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha) or size <= 0 or duration <= 0:
        raise ValueError("Story clip metadata is incomplete")
    media_id = metadata.get("source_media_object_id")
    media = None
    if media_id:
        media = db.get(MediaObject, media_id)
        if media is None or media.state != "ready" or media.sha256 != sha or media.size_bytes != size:
            raise ValueError("Story clip backup is incomplete")
    uri = f"local-cache://sha256/{sha}"
    identifier = "up_cut_" + digest([creation.id, role, sha])[:40]
    upload = db.get(CreatorUpload, identifier)
    if upload is None:
        upload = CreatorUpload(
            id=identifier,
            user_id=creation.user_id,
            storage_key=metadata.get("source_storage_key") or uri,
            source_local_uri=uri,
            source_sha256=sha,
            upload_transport="story-cut",
            origin="user_upload",
            original_filename=f"story-{role}.mp4",
            size_bytes=size,
            duration_ms=duration,
        )
    elif upload.user_id != creation.user_id or upload.source_sha256 != sha:
        raise ValueError("Story clip ownership is invalid")
    upload.normalization_status = "ready"
    upload.normalization_profile = "mobile-v1"
    upload.normalization_error = ""
    upload.playable_local_uri = uri
    upload.playable_media_object_id = media_id
    upload.playable_sha256 = sha
    upload.playable_size_bytes = size
    upload.duration_ms = duration
    db.add(upload)
    db.flush()
    if media is not None and media.visibility == "private":
        enqueue_private_media_prefetch(
            db,
            get_settings(),
            [media.object_key],
        )
    return identifier


def prepare_assets(db: Session, creation: CreatorCreation, request) -> None:
    # B and C may be handled by different workers. Serialize the one-time cut
    # so both use the same durable A/tail objects without duplicate uploads.
    locked = (
        db.query(CreatorCreation)
        .filter(CreatorCreation.id == creation.id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if locked is None or locked.user_id != creation.user_id:
        return
    creation = locked
    plan = deepcopy(creation.story_plan or {})
    if not plan or plan.get("status") == "draft":
        return
    if plan.get("source_tail_owner") and not plan.get("assets", {}).get("A"):
        upload = source_upload(db, creation)
        response = request(get_settings(), "POST", "/internal/v1/mobile-creator/story-clips",
                           timeout=150,
                           json={"source_uri": upload.playable_local_uri, "split_ms": plan["split_ms"],
                                 "creation_id": creation.id})
        metadata = response.json()
        # A user may have revised the plan while the deterministic cut ran.
        db.refresh(creation)
        if (creation.story_plan or {}).get("revision") != plan["revision"]:
            return
        plan["assets"] = {"A": register_cut(db, creation, metadata["A"], "A"),
                           plan["source_tail_owner"]: register_cut(db, creation, metadata["tail"], "tail")}
        creation.story_plan = plan
        db.commit()


def sync_story(db: Session, creation: CreatorCreation) -> None:
    plan = deepcopy(creation.story_plan or {})
    if creation.experience_mode != "story" or not plan or plan.get("status") == "draft" or creation.status in {"abandoned", "pending_review", "published"}:
        return
    states = story_out(db, creation)["clips"]
    if any(states[r]["status"] in {"failed", "cancelled", "expired"} for r in ROLES):
        plan["status"] = "partial_failure"
        creation.status = "source_ready"
    elif all(states[r]["status"] == "ready" for r in ("A", "B", "C")):
        assets = dict(plan["assets"])
        for role in ROLES:
            if role != plan["source_tail_owner"]:
                assets[role] = ending(db, plan, role).upload_id
        signature = digest([
            assets,
            plan.get("interaction_preset_id") or plan["interaction_type"],
            plan["revision"],
            plan["generation_ids"],
        ])
        version = db.query(CreatorVersion).filter_by(request_id=f"story-ready:{signature}").first()
        if version is None:
            a_upload = db.get(CreatorUpload, assets["A"])
            # Pause just before the physical end so ended cannot win the gate race.
            gate = max(0, a_upload.duration_ms - 80)
            interaction = {"gesture": plan["interaction_type"], "gate_at_ms": gate,
                           "gate_end_ms": gate + 4000, "pause_video": True,
                           "outcomes": {"success": {"action": "goto", "clip_id": "B"},
                                        "fail": {"action": "goto", "clip_id": "C"}}}
            try:
                preset = resolve_interaction_preset(plan)
            except ValueError as exc:
                raise RuntimeError("saved Story interaction preset is invalid") from exc
            apply_preset_fields(interaction, preset)
            source = {"entry_clip_id": "A", "clips": {
                role: {"timeline": {"media": {"duration_ms": db.get(CreatorUpload, assets[role]).duration_ms},
                                    "interactions": [interaction] if role == "A" else []},
                       **({"on_end": {"action": "end"}} if role != "A" else {})}
                for role in ("A", "B", "C")},
                "creator": {"assets": assets, "story_revision": plan["revision"]}}
            runtime = compile_runtime_spec(item_id=creation.id, content_mode="story", source=source, video_url="",
                video_urls={role: f"/api/v1/creator/previews/{identifier}" for role, identifier in assets.items()})
            number = int(db.query(func.max(CreatorVersion.number)).filter_by(creation_id=creation.id).scalar() or 0) + 1
            version = CreatorVersion(id=f"cv_{secrets.token_urlsafe(18)}", creation_id=creation.id,
                user_id=creation.user_id, number=number, request_id=f"story-ready:{signature}", brief="Story endings",
                status="ready", progress_stage="ready", progress_percent=100, source_timeline=source,
                runtime_spec=runtime, runtime_spec_version=runtime_spec_version_from_compiled(runtime), previewed_paths=[])
            db.add(version)
            db.flush()
        creation.active_version_id = version.id
        creation.source_timeline = version.source_timeline
        creation.runtime_spec = version.runtime_spec
        creation.runtime_spec_version = version.runtime_spec_version
        creation.status = "ready"
        creation.progress_percent = 100
        plan["status"] = "ready"
        for role in ROLES:
            item = ending(db, plan, role)
            if item:
                item.accepted_at = item.accepted_at or now()
    else:
        creation.status = "running"
        plan["status"] = "generating"
    creation.story_plan = plan
    creation.progress_stage = "ready" if plan["status"] == "ready" else "generating_endings"
    creation.updated_at = now()
    db.add(creation)
