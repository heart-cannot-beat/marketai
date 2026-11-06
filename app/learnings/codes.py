"""Asset short codes: the identifier a seller copies into a results sheet.

A seller never sees an AssetVersion id (front-flow contract §1-2). Every
generated image instead carries a short, printable code such as
``MK-7F3K2Q``: it is printed in the results template, the export manifest
and the learnings import preview, and the seller can paste it into the ad
name in Ads Manager -- an Ads Manager export then matches by ad name.

The code is a pure function of (Workspace, AssetVersion): 30 bits of a
SHA-256, Crockford base32 (no I / L / O / U), so no storage or migration is
needed and the same image always prints the same code. Resolution only
looks at one product's assets, so a collision is vanishingly rare; when it
happens the row is reported as ambiguous instead of guessed.
"""
from __future__ import annotations

import hashlib
import re

PREFIX = "MK-"
LENGTH = 6
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: Finds a code inside free text (an ad name such as "夏季_MK-7F3K2Q_v2").
CODE_PATTERN = re.compile(r"MK-([0-9A-HJKMNP-TV-Z]{6})(?![0-9A-Z])", re.IGNORECASE)


def asset_code(workspace_id: str, asset_version_id: str) -> str:
    digest = hashlib.sha256(f"marketai-asset-code:v1:{workspace_id}:{asset_version_id}".encode()).digest()
    value = int.from_bytes(digest[:4], "big") >> 2  # 30 bits
    chars = []
    for _ in range(LENGTH):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return PREFIX + "".join(reversed(chars))


def normalize_code(raw: str | None) -> str | None:
    """``mk-7f3k2q`` / ``MK7F3K2Q`` / ``7F3K2Q`` -> ``MK-7F3K2Q``; else None."""
    text = (raw or "").strip().upper().replace(" ", "")
    if text.startswith(PREFIX):
        text = text[len(PREFIX):]
    elif text.startswith("MK") and len(text) == LENGTH + 2:
        text = text[2:]
    if len(text) != LENGTH:
        return None
    text = text.replace("O", "0").replace("I", "1").replace("L", "1")
    match = CODE_PATTERN.fullmatch(PREFIX + text)
    return f"{PREFIX}{match.group(1).upper()}" if match else None


def codes_in_text(text: str | None) -> list[str]:
    """Every code mentioned in free text, in order, without duplicates."""
    found = [f"{PREFIX}{match.group(1).upper()}" for match in CODE_PATTERN.finditer(text or "")]
    return list(dict.fromkeys(found))
