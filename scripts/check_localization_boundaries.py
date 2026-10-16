#!/usr/bin/env python3
"""Validate server copy catalogs and the canonical-content translation boundary."""

from __future__ import annotations

import ast
import json
import re
import string
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
CATALOG_DIR = APP_DIR / "locales"
PROTECTED_TERMS = re.compile(r"\b(?:Pixo|Pixopixo|Credits)\b")
RETIRED_FILES = (
    APP_DIR / "public_text_export.py",
    APP_DIR / "public_text_translate.py",
)
RETIRED_SYMBOLS = (
    "apply_" + "translation_manifest",
    "Translation" + "ManifestError",
)


def supported_locales() -> tuple[str, ...]:
    tree = ast.parse((APP_DIR / "locale.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            is_supported_locales = any(
                isinstance(target, ast.Name) and target.id == "SUPPORTED_LOCALES"
                for target in node.targets
            )
            if is_supported_locales:
                value = ast.literal_eval(node.value)
                if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
                    return value
    raise ValueError("SUPPORTED_LOCALES must be a literal tuple")


def flatten(value: Any, *, prefix: str = "") -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError(f"catalog node must be an object: {prefix or '<root>'}")
    output: dict[str, str] = {}
    for key, child in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"catalog key must be non-empty text: {prefix or '<root>'}")
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(child, dict):
            output.update(flatten(child, prefix=path))
        elif isinstance(child, str) and child.strip():
            output[path] = child
        else:
            raise ValueError(f"catalog value must be non-empty text: {path}")
    return output


def placeholders(value: str) -> set[str]:
    return {
        field_name
        for _literal, field_name, _format_spec, _conversion in string.Formatter().parse(value)
        if field_name is not None
    }


def validate_catalogs() -> list[str]:
    errors: list[str] = []
    locales = supported_locales()
    actual_files = {path.stem for path in CATALOG_DIR.glob("*.json")}
    if actual_files != set(locales):
        missing = sorted(set(locales) - actual_files)
        extra = sorted(actual_files - set(locales))
        if missing:
            errors.append(f"missing server locale catalogs: {', '.join(missing)}")
        if extra:
            errors.append(f"unsupported server locale catalogs: {', '.join(extra)}")
        return errors

    catalogs: dict[str, dict[str, str]] = {}
    for locale in locales:
        path = CATALOG_DIR / f"{locale}.json"
        try:
            catalogs[locale] = flatten(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: {exc}")

    source = catalogs.get("en")
    if source is None:
        return errors
    source_keys = set(source)
    for locale, catalog in catalogs.items():
        if set(catalog) != source_keys:
            missing = sorted(source_keys - set(catalog))
            extra = sorted(set(catalog) - source_keys)
            if missing:
                errors.append(f"{locale} missing keys: {', '.join(missing)}")
            if extra:
                errors.append(f"{locale} extra keys: {', '.join(extra)}")
            continue
        for key, source_value in source.items():
            translated = catalog[key]
            if placeholders(translated) != placeholders(source_value):
                errors.append(f"{locale}:{key} changed placeholders")
            for term in PROTECTED_TERMS.findall(source_value):
                if term not in translated:
                    errors.append(f"{locale}:{key} changed protected term {term}")
    return errors


def validate_retired_pipeline() -> list[str]:
    errors = [
        f"retired canonical-overwrite tool exists: {path.relative_to(ROOT)}"
        for path in RETIRED_FILES
        if path.exists()
    ]
    for base in (APP_DIR, ROOT / "scripts"):
        for path in base.rglob("*.py"):
            if path.resolve() == Path(__file__).resolve():
                continue
            source = path.read_text(encoding="utf-8")
            for symbol in RETIRED_SYMBOLS:
                if symbol in source:
                    errors.append(
                        f"retired canonical-overwrite symbol {symbol} found in "
                        f"{path.relative_to(ROOT)}"
                    )
    return errors


def main() -> int:
    errors = validate_catalogs() + validate_retired_pipeline()
    if errors:
        for error in errors:
            print(f"[localization] {error}", file=sys.stderr)
        return 1
    print("[localization] server catalogs and content boundaries are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
