from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path

import geoip2.database
from geoip2.errors import AddressNotFoundError


@lru_cache(maxsize=4)
def _reader(database_path: str) -> geoip2.database.Reader | None:
    path = Path(database_path)
    if not database_path or not path.is_file():
        return None
    return geoip2.database.Reader(path)


def country_code_for_ip(ip_value: str | None, *, database_path: str) -> str | None:
    """Read only a country code from the local MMDB; never retain the address."""
    try:
        address = ipaddress.ip_address(str(ip_value or "").strip())
    except ValueError:
        return None
    if address.is_private or address.is_loopback or address.is_unspecified:
        return None
    reader = _reader(database_path)
    if reader is None:
        return None
    try:
        result = reader.country(str(address))
    except (AddressNotFoundError, ValueError):
        return None
    code = result.country.iso_code
    return code.upper() if isinstance(code, str) and len(code) == 2 else None
