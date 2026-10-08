"""Idempotently provision the isolated prelaunch social seed account pool."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.oss_storage import (
    OssObjectNotFoundError,
    head_object,
    object_key,
    public_url,
    upload_bytes,
)

DICEBEAR_VERSION = "10.x"
DICEBEAR_LICENSE = "CC0 1.0"
DICEBEAR_LICENSE_SOURCE = "https://github.com/dicebear/styles/blob/main/LICENSE.md"
DEFAULT_SOCIAL_SEED_BATCH = "prelaunch-v1"
SOCIAL_SEED_PURPOSE = "social_seed"
PROFILE_VERSION = "natural-v2"
AVATAR_STYLES = (
    "cameo",
    "clay",
    "cutouts",
    "gaze",
    "initial-face",
    "landscape",
    "line-face",
    "lorelei",
    "moods",
    "notionists",
    "open-peeps",
    "pixel-art",
)
NICKNAMES = (
    "Mia", "noahhere", "EllieMay", "sam_wanders", "LucasNorth",
    "zoeafterdark", "milo", "NinaNextDoor", "julesonline", "alexcloudy",
    "Chloe", "benji", "SophieLane", "theohere", "maya_moves",
    "Finn", "LenaLately", "owenoutside", "rubyroo", "Max",
    "Ivy", "calebcreates", "Tessa", "jamieinmotion", "LeoLoops",
    "hannahhere", "Arlo", "emmaday", "NateWanders", "lucyblue",
    "Kai", "oliviaonline", "Remy", "isaacafterhours", "Sadie",
    "ethanplays", "Cleo", "dylandoesstuff", "Mae", "tommytracks",
    "Ava", "eli_outside", "Poppy", "masonmornings", "Lila",
    "charlieish", "Nora", "rowanroams", "Millie", "jacobnextdoor",
    "Stella", "louiearound", "Alice", "loganlate", "Freya",
    "harryhere", "Eden", "oscaroffline", "Rosie", "devonday",
    "Grace", "milesaway", "Isla", "rileyaround", "Daisy",
    "coleontheroad", "June", "parkerplays", "Hazel", "sunny_sam",
    "Wren", "averymaybe", "Bonnie", "jessehere", "Esme",
    "cameroncoast", "Olive", "jordanwanders", "Phoebe", "ashbyday",
    "Mabel", "taylortakes", "Sienna", "caseyoutside", "Hallie",
    "drewdaily", "Flora", "robinroundhere", "Cora", "blakeonbreak",
    "Elsie", "quinnquietly", "Thea", "morganmoves", "Imogen",
    "roryonline", "Faye", "skylarside", "Willa", "kitandcoffee",
    # --- expansion to 250+ (appended; do NOT reorder existing entries:
    # account index -> nickname must stay stable for existing accounts) ---
    "lilywaves", "marcusjune", "orla_sky", "devonreef",
    "kaitowrites", "RowanFrost", "niallbloom", "paisleyq",
    "soren_vale", "MiaWanders", "julesvine", "harperlee",
    "theo_wild", "nadiaskybook", "corbin_tide", "elsiesnow",
    "robinbanksx", "FinnickRay", "marlowe_q", "daxatmoss",
    "wrensoflight", "safiya_kai", "hugo_marlowe", "ivyredwood",
    "theo_north", "lucymeadow", "arjunwaves", "nora_skyfall",
    "felixharbor", "maeve_bell", "oscarwestwood", "juneberryx",
    "leowildfire", "tessmoonrise", "kai_breaker", "sadieatlas",
    "milogrey", "lucas_frost", "emmastonex", "noahdrift",
    "avaeverest", "liamnorthx", "zoeatdusk", "benjiwaves",
    "chloe_harper", "mollysunray", "dylanreef", "calebstorm",
    "rubyatlasx", "ellie_moon", "maxwellgreen", "ivy_north",
    "jasperwaves", "sophieholly", "theo_rain", "ninaatlasx",
    "arlo_frost", "lena_wilde", "owenreef", "mia_parker",
    "finnharbor", "cleomoonx", "ethanwilde", "daisy_frost",
    "jacobreef", "stellanorth", "harrywaves", "rosieredwood",
    "grace_atlas", "charlie_moon", "masonfrost", "lilyharbor",
    "nora_wilde", "parkerreef", "hazelstone", "wrenadrift",
    "sunny_wren", "bonnie_moon", "oliveatlas", "jesse_reed",
    "esmefrost", "cameronnorth", "oliveree", "phoebewaves",
    "ash_harbor", "mabelmoon", "taylorreef", "siennafrost",
    "casey_wilde", "hallie_reed", "drewatlas", "flora_north",
    "robinreef", "corastone", "blake_moon", "elsiewilde",
    "quinnreef", "theafrost", "morganharbor", "rory_north",
    "fayeatlas", "skylarreef", "willa_moon", "kit_harbor",
    "lilyfrost", "marcus_wave", "orla_north", "devon_moon",
    "kai_reef", "rowan_wild", "niall_frost", "paisley_moon",
    "soren_harbor", "mia_wilde", "jules_north", "harper_reef",
    "theo_moon", "nadia_frost", "corbin_wild", "elsie_harbor",
    "robin_moon", "finnick_north", "marlowe_frost", "dax_wilde",
    "wren_reef", "safiya_north", "hugo_moon", "ivy_frost",
    "lucy_harbor", "arjun_moon", "nora_reef", "felix_north",
    "maeve_frost", "oscar_moon", "june_reef", "leo_north",
    "tess_frost", "kai_moon", "sadie_wren", "milo_harbor",
    "lucas_moon", "emma_frost", "noah_wilde", "ava_north",
    "liam_reef", "zoe_moon", "benji_frost", "chloe_harbor",
    "molly_north", "dylan_moon",
)


def account_specs(batch_id: str, count: int) -> list[dict[str, str]]:
    if count > len(NICKNAMES):
        raise ValueError(f"only {len(NICKNAMES)} curated nicknames are available")
    invalid = [        nickname
        for nickname in NICKNAMES
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,31}", nickname) is None
    ]
    if invalid:
        raise ValueError(f"invalid curated nicknames: {invalid}")
    specs: list[dict[str, str]] = []
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in batch_id)
    for index in range(count):
        number = index + 1
        style = AVATAR_STYLES[(index * 5 + 3) % len(AVATAR_STYLES)]
        specs.append({
            "user_id": f"social-seed-{slug}-{number:03d}",
            "nickname": NICKNAMES[index],
            "avatar_seed": f"pixo-social-seed-{PROFILE_VERSION}-{batch_id}-{number:03d}",
            "avatar_style": style,
        })
    return specs


def _download_avatar(style: str, seed: str) -> bytes:
    url = f"https://api.dicebear.com/{DICEBEAR_VERSION}/{style}/png"
    for attempt in range(5):
        response = requests.get(url, params={"seed": seed, "size": 256}, timeout=30)
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 4:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After", "").strip()
            delay = float(retry_after) if retry_after.isdigit() else min(2 ** attempt, 8)
            time.sleep(delay)
            continue
        response.raise_for_status()
        payload = response.content
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("DiceBear returned a non-PNG avatar")
        return payload
    raise RuntimeError("DiceBear avatar download retries exhausted")


def _existing_sha256(settings, key: str) -> str | None:
    try:
        metadata = head_object(settings, key=key)
    except OssObjectNotFoundError:
        return None
    return metadata.headers.get("x-oss-meta-sha256") or ""


def provision(*, batch_id: str, count: int, manifest_path: Path) -> list[dict[str, str]]:
    from app.db import SessionLocal
    from app.models import User

    if count < 1 or count > len(NICKNAMES):
        raise ValueError(f"count must be between 1 and {len(NICKNAMES)}")
    settings = get_settings()
    if settings.media_storage_mode.strip().lower() != "oss":
        raise RuntimeError("social seed avatars must be provisioned with MEDIA_STORAGE_MODE=oss")

    manifest: list[dict[str, str]] = []
    with SessionLocal() as db:
        for spec in account_specs(batch_id, count):
            key = object_key(
                settings,
                "internal",
                "social-seed",
                batch_id,
                "avatars",
                PROFILE_VERSION,
                f"{spec['user_id']}.png",
            )
            digest = _existing_sha256(settings, key)
            if digest is None:
                avatar = _download_avatar(spec["avatar_style"], spec["avatar_seed"])
                digest = hashlib.sha256(avatar).hexdigest()
                upload_bytes(
                    settings,
                    key=key,
                    payload=avatar,
                    content_type="image/png",
                    public=True,
                    immutable=True,
                    extra_headers={
                        "x-oss-meta-sha256": digest,
                        "x-oss-meta-dicebear-version": DICEBEAR_VERSION,
                        "x-oss-meta-dicebear-style": spec["avatar_style"],
                        "x-oss-meta-profile-version": PROFILE_VERSION,
                    },
                )
            if not digest:
                raise RuntimeError(f"avatar is missing sha256 metadata: {key}")
            stored = head_object(settings, key=key)
            if (
                stored.size_bytes <= 0
                or stored.content_type.split(";")[0].lower() != "image/png"
                or stored.headers.get("x-oss-meta-sha256") != digest
            ):
                raise RuntimeError(f"avatar verification failed: {key}")

            row = db.get(User, spec["user_id"])
            if row is None:
                row = User(
                    user_id=spec["user_id"],
                    provider="internal",
                    subject=f"social-seed:{batch_id}:{spec['user_id']}",
                    enabled=True,
                    nickname=spec["nickname"],
                    avatar_url=public_url(settings, key),
                    bio="Prelaunch interaction preview account",
                    source="admin",
                    internal_purpose=SOCIAL_SEED_PURPOSE,
                    internal_batch=batch_id,
                    created_at=datetime.now(timezone.utc),
                )
                db.add(row)
            else:
                if (
                    row.source != "admin"
                    or row.internal_purpose != SOCIAL_SEED_PURPOSE
                    or row.internal_batch != batch_id
                ):
                    raise RuntimeError(f"refusing to repurpose existing account: {row.user_id}")
                row.enabled = True
                row.nickname = spec["nickname"]
                row.avatar_url = public_url(settings, key)
            db.flush()
            manifest.append({
                **spec,
                "profile_version": PROFILE_VERSION,
                "batch_id": batch_id,
                "dicebear_version": DICEBEAR_VERSION,
                "dicebear_style": spec["avatar_style"],
                "license": DICEBEAR_LICENSE,
                "license_source": DICEBEAR_LICENSE_SOURCE,
                "oss_key": key,
                "sha256": digest,
                "avatar_url": public_url(settings, key),
            })
        db.commit()

    with SessionLocal() as db:
        total = db.query(User).filter(
            User.internal_purpose == SOCIAL_SEED_PURPOSE,
            User.internal_batch == batch_id,
        ).count()
    if total < count:
        raise RuntimeError(
            f"batch must contain at least {count} accounts, found {total}"
        )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"batch_id": batch_id, "accounts": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", default=DEFAULT_SOCIAL_SEED_BATCH)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/social-seed/prelaunch-v1-manifest.json"),
    )
    args = parser.parse_args()
    rows = provision(batch_id=args.batch_id, count=args.count, manifest_path=args.manifest)
    print(f"social seed ready: batch={args.batch_id} accounts={len(rows)}")


if __name__ == "__main__":
    main()
