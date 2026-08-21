#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HTML_CREATOR_NICKNAMES = (
    "VelvetOrbit",
    "NeonHarbor",
    "PixelNomad",
    "LunarMoth",
    "EchoJuniper",
    "CosmicKite",
    "MarbleFox",
    "StaticBloom",
    "IndigoDrift",
    "AmberCircuit",
    "NovaThimble",
    "SilverMango",
    "CloudRascal",
    "MossyComet",
    "QuartzWhale",
    "MidnightPollen",
    "SolarBadger",
    "FableCurrent",
    "CopperSparrow",
    "PrismCoyote",
    "DuskLantern",
    "PaperMeteor",
    "CobaltOtter",
    "DreamParallax",
    "MintVoyager",
    "CrimsonPebble",
    "GlitchMeadow",
    "OrbitBiscuit",
    "VioletTide",
    "SatinRocket",
    "NimbusPanda",
    "ElectricFern",
    "GoldenStatic",
    "MoonlitGecko",
    "CoralCipher",
    "WanderingPixel",
    "BlueberryNova",
    "VelcroGalaxy",
    "TinyMonsoon",
    "ChromeWillow",
    "AstralPigeon",
    "BambooSignal",
    "FrostedLynx",
    "CinderJelly",
    "PlasmaTulip",
    "QuietSatellite",
    "MirageRaccoon",
    "PastelQuasar",
    "RubyTundra",
    "TurboAcorn",
    "OpalFrequency",
    "SunsetWalrus",
    "LaserDandelion",
    "MeteorMuffin",
    "JadeVortex",
    "PocketAurora",
    "CloudyJaguar",
    "SonicMarigold",
    "MagentaTrail",
    "OrbitingToast",
    "CrystalWombat",
    "BananaNebula",
    "FlannelPhoton",
    "ArcticNoodle",
    "ElectricMarmot",
    "PeachyMatrix",
    "VelvetTornado",
    "CometCactus",
    "MellowVoltage",
    "IndigoPancake",
    "GraniteButterfly",
    "NeonSeashell",
    "CosmicTurnip",
    "PixelFirefly",
    "LemonEclipse",
    "MoonberrySignal",
    "PrismLobster",
    "StaticKiwi",
    "DoodleAsteroid",
    "CopperCloud",
    "AuroraPickle",
    "VaporRobin",
    "MidnightMochi",
    "QuartzRabbit",
    "SolarSprout",
    "EchoPapaya",
    "CobaltCricket",
    "FuzzyHorizon",
    "MintySupernova",
    "MarbleKoala",
    "DigitalClover",
    "AmberYeti",
    "LunarPretzel",
    "SapphireMoth",
    "WavyPhoton",
    "CoralParadox",
    "SilverFirework",
    "NovaNightingale",
    "MossyArcade",
    "VelvetSunbeam",
)


@dataclass(frozen=True)
class HtmlCreator:
    user_id: str
    nickname: str

    def payload(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "provider": "content_pool",
            "subject": f"pixo-html-content-pool/{self.user_id}",
            "enabled": True,
            "nickname": self.nickname,
            "avatar_url": "",
            "bio": "Interactive maker on Pixo",
        }


def html_creators() -> tuple[HtmlCreator, ...]:
    if len(HTML_CREATOR_NICKNAMES) != 100:
        raise RuntimeError("HTML creator nickname catalog must contain exactly 100 entries")
    if len(set(HTML_CREATOR_NICKNAMES)) != len(HTML_CREATOR_NICKNAMES):
        raise RuntimeError("HTML creator nicknames must be unique")
    return tuple(
        HtmlCreator(user_id=f"html_creator_{index:03d}", nickname=nickname)
        for index, nickname in enumerate(HTML_CREATOR_NICKNAMES, start=1)
    )


def upsert_creator(*, backend_url: str, publish_key: str, creator: HtmlCreator) -> dict[str, Any]:
    request = Request(
        f"{backend_url.rstrip('/')}/internal/v1/users",
        data=json.dumps(creator.payload(), ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Publish-Key": publish_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{creator.user_id}: user API returned HTTP {exc.code}: {detail[:500]}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"{creator.user_id}: user API unavailable: {exc.reason}") from exc
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{creator.user_id}: user API returned invalid JSON") from exc
    if result.get("user_id") != creator.user_id or result.get("source") != "admin":
        raise RuntimeError(f"{creator.user_id}: user API returned unexpected identity")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Idempotently create the 100 fixed Pixo HTML content-pool authors.",
    )
    parser.add_argument(
        "--backend-url",
        default=os.getenv("PIXO_BACKEND_URL", ""),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    creators = html_creators()
    if args.dry_run:
        print(json.dumps([creator.payload() for creator in creators], ensure_ascii=False, indent=2))
        return 0

    backend_url = args.backend_url.strip()
    publish_key = os.getenv("PIXO_PUBLISH_KEY", "").strip()
    if not backend_url or not publish_key:
        parser.error("PIXO_BACKEND_URL/--backend-url and PIXO_PUBLISH_KEY are required")
    for creator in creators:
        upsert_creator(
            backend_url=backend_url,
            publish_key=publish_key,
            creator=creator,
        )
    print(json.dumps({"upserted": len(creators)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"seed-html-creators: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
