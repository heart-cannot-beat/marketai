"""What a pasted competitor input is (spec §4.1), decided without any paid call.

==========================================  ==============  =====================
input                                       kind            yields
==========================================  ==============  =====================
``facebook.com/ads/library/?view_all_page_id=<id>``  ``ad_library``   page id
``facebook.com/profile.php?id=<id>``,       ``facebook_page``  page id
``facebook.com/people/<name>/<id>``,
``facebook.com/<name>-<id>``, ``facebook.com/<id>``
``facebook.com/<alias>``                    ``facebook_page``  alias only
anything else that is not a URL or domain   ``name``        the name
==========================================  ==============  =====================

A link carrying a page id confirms the page on create (§4.1 "是"). A
``facebook.com/<alias>`` link names the page but not its id, and a scan
needs the id (``ads_for_page``), so the alias is kept and a resolve looks it
up: a candidate whose alias equals the pasted one is that page. Another
URL, or a bare domain, is an official site, not a competitor: it goes into
``website`` next to a name.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from app.competitors.normalize import landing_domain

KIND_NAME = "name"
KIND_FACEBOOK_PAGE = "facebook_page"
KIND_AD_LIBRARY = "ad_library"

FACEBOOK_DOMAINS = frozenset({"facebook.com", "fb.com"})
#: Sites that are never an official website (a page link belongs in ``input``).
NOT_A_WEBSITE = FACEBOOK_DOMAINS | {"instagram.com", "fb.me", "messenger.com"}

MAX_NAME_LENGTH = 200
MAX_ALIAS_LENGTH = 100

#: First path segments that are Facebook features, not page aliases.
_RESERVED_SEGMENTS = frozenset({
    "ads", "pages", "people", "profile.php", "groups", "events", "watch", "marketplace",
    "gaming", "help", "login", "login.php", "share", "sharer", "sharer.php", "photo.php",
    "permalink.php", "story.php", "home.php", "search", "hashtag", "reel", "reels",
    "stories", "business", "privacy", "policies", "settings", "notifications",
    "messages", "friends", "bookmarks", "fundraisers", "l.php", "dialog", "plugins",
    "tr", "media", "photos", "videos", "live", "public", "legal", "about", "careers",
})
_ALIAS = re.compile(r"[A-Za-z0-9.]{1,%d}" % MAX_ALIAS_LENGTH)
_NAME_WITH_ID = re.compile(r".+-(\d{5,32})")
_DIGITS = re.compile(r"\d{5,32}")
_DOMAIN_LIKE = re.compile(
    r"(?i)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}(?::\d{1,5})?(?:[/?#]\S*)?"
)


class CompetitorInputError(ValueError):
    """A refused input; ``code`` and ``params`` are what the interface renders."""

    def __init__(self, code: str, message: str, params: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.params = params or {}


@dataclass(frozen=True)
class ParsedInput:
    kind: str
    text: str
    name: str | None = None
    page_id: str | None = None
    page_alias: str | None = None

    @property
    def is_link(self) -> bool:
        return self.kind != KIND_NAME


def _url(text: str):
    candidate = text if re.match(r"(?i)https?://", text) else f"https://{text}"
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    return parts if host else None


def _is_url_like(text: str) -> bool:
    return bool(re.match(r"(?i)https?://", text)) or bool(_DOMAIN_LIKE.fullmatch(text))


def _facebook_link(text: str, parts) -> ParsedInput:
    query = parse_qs(parts.query)
    segments = [segment for segment in parts.path.split("/") if segment]
    first = segments[0] if segments else ""
    if first == "ads" and segments[1:2] == ["library"]:
        page_id = (query.get("view_all_page_id") or [""])[0].strip()
        if _DIGITS.fullmatch(page_id):
            return ParsedInput(KIND_AD_LIBRARY, text, page_id=page_id)
    elif first == "profile.php":
        page_id = (query.get("id") or [""])[0].strip()
        if _DIGITS.fullmatch(page_id):
            return ParsedInput(KIND_FACEBOOK_PAGE, text, page_id=page_id)
    elif first in ("people", "pages") and len(segments) >= 3 and _DIGITS.fullmatch(segments[2]):
        return ParsedInput(KIND_FACEBOOK_PAGE, text, page_id=segments[2])
    elif first and first.lower() not in _RESERVED_SEGMENTS:
        if _DIGITS.fullmatch(first):
            return ParsedInput(KIND_FACEBOOK_PAGE, text, page_id=first)
        with_id = _NAME_WITH_ID.fullmatch(first)
        if with_id:
            return ParsedInput(KIND_FACEBOOK_PAGE, text, page_id=with_id.group(1))
        if _ALIAS.fullmatch(first) and not first.startswith(".") and not first.endswith("."):
            return ParsedInput(KIND_FACEBOOK_PAGE, text, page_alias=first)
    raise CompetitorInputError(
        "competitor_link_unrecognized",
        "the Facebook link names no page: paste the page link or its Ad Library link",
    )


def normalize_name(value: str) -> str:
    """Case, spaces and punctuation ignored (§4.2 step 2): ``"Bright Volt"`` -> ``"brightvolt"``."""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def clean_name(value: str) -> str:
    return " ".join(value.split())


def parse_input(text: str) -> ParsedInput:
    """Classify one pasted input; raises :class:`CompetitorInputError`."""
    stripped = (text or "").strip()
    if not stripped:
        raise CompetitorInputError("competitor_input_required", "paste a page link or type a name")
    if _is_url_like(stripped):
        parts = _url(stripped)
        domain = landing_domain(parts.geturl()) if parts is not None else None
        if domain in FACEBOOK_DOMAINS:
            return _facebook_link(stripped, parts)
        raise CompetitorInputError(
            "competitor_name_required",
            "a website alone cannot be searched: type the competitor's name and put the site in website",
            {"website_domain": domain} if domain else {},
        )
    name = clean_name(stripped)
    if len(name) > MAX_NAME_LENGTH:
        raise CompetitorInputError(
            "competitor_name_too_long", f"a name has at most {MAX_NAME_LENGTH} characters",
            {"limit": MAX_NAME_LENGTH},
        )
    if not normalize_name(name):
        raise CompetitorInputError("competitor_input_required", "the name has no letters or digits")
    return ParsedInput(KIND_NAME, stripped, name=name)


def parse_page_link(text: str) -> ParsedInput:
    """A confirmation's ``page_url``: a Facebook link that carries a page id."""
    stripped = (text or "").strip()
    parts = _url(stripped) if _is_url_like(stripped) else None
    if parts is None or landing_domain(parts.geturl()) not in FACEBOOK_DOMAINS:
        raise CompetitorInputError(
            "competitor_link_unrecognized",
            "the link names no page: paste the page link or its Ad Library link",
        )
    parsed = _facebook_link(stripped, parts)
    if parsed.page_id is None:
        raise CompetitorInputError(
            "competitor_link_needs_page_id",
            "this link names the page by alias only; paste its Ad Library link or resolve it",
            {"page_alias": parsed.page_alias},
        )
    return parsed


def normalize_website(text: str | None) -> str | None:
    """The registrable domain of an official site (``https://www.acme-charge.example/x`` -> ``acme-charge.example``)."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    parts = _url(stripped) if _is_url_like(stripped) else None
    domain = landing_domain(parts.geturl()) if parts is not None else None
    if not domain or "." not in domain or domain in NOT_A_WEBSITE:
        raise CompetitorInputError(
            "competitor_website_invalid",
            "website must be the competitor's own site, as a link or a domain",
        )
    return domain
