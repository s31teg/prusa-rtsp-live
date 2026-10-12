"""Camera token parsing.

Accepts what Prusa Connect hands out for a camera:
  * the bare 20-character token,
  * the web camera link (``https://camera-service-webcam.prusa3d.com/#t=v1.<...>``
    with an obfuscated token, or the older ``?token=<token>`` form),
  * the JSON from a camera pairing QR code (``{"token": "..."}``).

The ``v1.`` obfuscation mirrors Prusa's web camera page: base64url, then XOR
with a static key published in that page's config.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Optional
from urllib.parse import parse_qs, urlsplit

DEFAULT_OBFUSCATION_KEY = "webcam-obfuscation-prod-v1-fusekle"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{20}$")
_PREFIX = "v1."


def is_token(value: str) -> bool:
    return bool(_TOKEN_RE.match(value or ""))


def deobfuscate(value: str, key: str = DEFAULT_OBFUSCATION_KEY) -> Optional[str]:
    if not value.startswith(_PREFIX) or not key:
        return None
    raw = value[len(_PREFIX):].replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    try:
        data = base64.b64decode(raw, validate=True)
    except ValueError:
        return None
    out = "".join(chr(b ^ ord(key[i % len(key)])) for i, b in enumerate(data))
    return out if is_token(out) else None


def obfuscate(token: str, key: str = DEFAULT_OBFUSCATION_KEY) -> str:
    """Inverse of :func:`deobfuscate` (used by tests)."""
    data = bytes(ord(c) ^ ord(key[i % len(key)]) for i, c in enumerate(token))
    return _PREFIX + base64.urlsafe_b64encode(data).decode().rstrip("=")


def parse_token(value: str, key: str = DEFAULT_OBFUSCATION_KEY) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    if is_token(value):
        return value
    if value.startswith("{"):
        try:
            data = json.loads(value)
        except ValueError:
            return None
        token = data.get("token") if isinstance(data, dict) else None
        return token if isinstance(token, str) and is_token(token) else None
    if "://" in value:
        parts = urlsplit(value)
        fragment = parse_qs(parts.fragment.lstrip("#"))
        if "t" in fragment:
            return deobfuscate(fragment["t"][0], key)
        query = parse_qs(parts.query)
        if "token" in query and is_token(query["token"][0]):
            return query["token"][0]
        return None
    if value.startswith(_PREFIX):
        return deobfuscate(value, key)
    return None


def mask(token: str) -> str:
    if not token:
        return "<none>"
    return token[:4] + "…" + token[-2:]
