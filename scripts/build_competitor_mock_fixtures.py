"""Build the competitor-scan Controlled Mock fixtures from the P0 raw run.

    .venv/bin/python -m scripts.build_competitor_mock_fixtures \\
        --raw-dir <main checkout>/data/p0-scrapecreators/runs/20261026-163158 \\
        --fictional-map <the private fictional map kept beside the raw run>

The raw run and the fictional map are git-ignored: they hold the real
brands, ad copy, signed media links, avatars and personal accounts. Only
this script's output, under
``app/competitors/sources/fixtures/scrapecreators_p0/``, is committed, and
this script itself names no brand and no person: both are recognized from
the data and the private map. Rules (Issue #268, fictional brands #428):

* kept as recorded: page categories, like counts, dates, formats,
  platforms, collation, card counts, result counts and pagination -- the
  structure downstream acceptance asserts on;
* every brand is fictional (the map, applied in this order): ad copy
  (titles, bodies, link descriptions, captions) is replaced whole by
  fictional copy that trips the same Controlled Mock keyword rules; landing
  links become ``*.example`` links; page-search candidates get fictional
  names, aliases and Instagram names of the same name-match tier;
  advertiser names, profile links and queries are rewritten by substring;
  page, ad and collation ids become synthetic ids of the same length and
  the same string order (the synthetic ids below stay as they are);
* personal advertisers (Meta page category ``Profile``: people selling on
  Marketplace) become ``Person Example <n>`` with synthetic page, ad,
  collation and Marketplace ids; their bodies, which carried a VIN and a
  home town, become a one-line note built from the kept title;
* page-search candidates named after a person (their page ids are listed
  in the private map) become ``Person Example <n>`` too;
* creators in partnership ads (a ``snapshot`` or ``branded_content`` page
  that is not the advertiser) are people beyond the advertiser (spec §10)
  and become ``Creator Example <n>`` with synthetic ids and aliases;
* signed media links (fbcdn) become placeholder links keyed by a hash of
  the original path, keeping the original ``oe=`` expiry; avatars
  (``page_profile_picture_url``, ``image_uri`` ...) are removed;
* landing links lose their query string and fragment (tracking
  parameters); Ad Library links keep only ``id``;
* pagination cursors become readable synthetic tokens.

The P0 run never saw an empty page, a 402, a 404 or an ``/ad`` answer;
those templates are synthesized here (``provenance.kind = "synthesized"``)
and the ``/ad`` shape is synthesized at runtime by the mock.

The build fails if a copy value or a landing link has no fictional
counterpart in the map, or if any output still contains a signed CDN link,
an avatar, an API key field, a tracking query string, any string it
scrubbed or replaced, or a pattern the map forbids.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from PIL import Image

from app.competitors.normalize import media_url_expires_at
from app.competitors.sources.mock import FIXTURE_DIR, MOCK_MEDIA_HOST, placeholder_media_url

RUN_ID = "p0-scrapecreators/20261026-163158"
#: The run started 2026-10-26 16:31:58 +0800; its calls span about a minute.
CAPTURED_AT = datetime(2026, 10, 26, 8, 31, 58, tzinfo=timezone.utc)

#: raw call number -> fixture name, in call order (cursor tokens are mapped in
#: order). The raw file names carry the real queries, so only the number is used.
RAW_CALLS = (
    ("002", "search-companies--acme-charge"),
    ("003", "company-ads--acme-charge--p1"),
    ("004", "company-ads--acme-charge--p2"),
    ("005", "company-ads--acme-charge--p3"),
    ("006", "search-companies--brightvolt"),
    ("007", "company-ads--brightvolt--p1"),
    ("008", "search-companies--halcyon-motors"),
    ("009", "company-ads--halcyon-motors--p1"),
    ("010", "search-ads--nacs-adapter--p1"),
    ("011", "search-ads--nacs-adapter--p2"),
    ("012", "search-ads--ccs1-adapter--p1"),
    ("013", "search-ads--ccs1-adapter--p2"),
)

PERSONAL_PAGE_CATEGORY = "Profile"
PERSON_ID_BASE = 900000000000000
CREATOR_ID_BASE = 950000000000000
PERSONAL_AD_ID_BASE = 910000000000000
PERSONAL_COLLATION_ID_BASE = 920000000000000
MARKETPLACE_ITEM_ID_BASE = 930000000000000

AVATAR_KEYS = frozenset({"page_profile_picture_url", "page_profile_pic_url", "image_uri", "instagram_profile_pic_url"})
MEDIA_KEYS = {
    "original_image_url": "image",
    "resized_image_url": "image",
    "watermarked_resized_image_url": "image",
    "video_preview_image_url": "video-poster",
    "video_hd_url": "video",
    "video_sd_url": "video",
    "watermarked_video_hd_url": "video",
    "watermarked_video_sd_url": "video",
    "video_hd_handle": "video",
    "video_sd_handle": "video",
}
LINK_KEYS = frozenset({"link_url", "extra_links", "url"})
CDN_HOST = re.compile(r"fbcdn\.net|cdninstagram\.com|fbsbx\.com", re.IGNORECASE)
URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\]+")
#: Scrubbed body lines shorter than this are too generic to audit for.
MIN_AUDITED_LINE = 16

#: Ad copy, replaced whole by its fictional counterpart.
COPY_KEYS = frozenset({"title", "body", "text", "link_description", "caption"})
#: Meta's own hosts: their links keep their shape, only names and ids change.
PLATFORM_HOSTS = frozenset({"facebook.com", "www.facebook.com", "fb.com", "www.instagram.com"})
ID_KEYS = frozenset({"page_id", "ad_archive_id", "collation_id", "pageId"})
PROFILE_KEYS = frozenset({"page_profile_uri"}) | LINK_KEYS
LONG_NUMBER = re.compile(r"(?<!\d)\d{8,}(?!\d)")
CANDIDATE_FIELDS = ("name", "page_alias", "ig_username")
#: Candidate aliases shorter than this are not audited for (see ``_fictional_item``).
MIN_AUDITED_ALIAS = 8


@dataclass(frozen=True)
class Pseudonym:
    name: str
    page_id: str
    alias: str


class Pseudonyms:
    """Stable pseudonyms in order of first appearance, plus what they replaced."""

    def __init__(self) -> None:
        self.people: dict[str, Pseudonym] = {}
        self.creators: dict[str, Pseudonym] = {}
        self.personal_ads: dict[str, int] = {}
        #: Every real string replaced; none may survive in any output.
        self.scrubbed: set[str] = set()

    def person(self, real_id: str) -> Pseudonym:
        if real_id not in self.people:
            n = len(self.people) + 1
            self.people[real_id] = Pseudonym(f"Person Example {n}", str(PERSON_ID_BASE + n), f"person.example.{n}")
        return self.people[real_id]

    def creator(self, real_id: str) -> Pseudonym:
        if real_id not in self.creators:
            n = len(self.creators) + 1
            self.creators[real_id] = Pseudonym(f"Creator Example {n}", str(CREATOR_ID_BASE + n), f"creator.example.{n}")
        return self.creators[real_id]

    def personal_ad(self, real_ad_id: str) -> int:
        return self.personal_ads.setdefault(real_ad_id, len(self.personal_ads) + 1)

    def scrub(self, *values: Any) -> None:
        self.scrubbed.update(value for value in values if isinstance(value, str) and value.strip())

    def synthetic_ids(self) -> set[str]:
        """Every id this build made up; the fictional ids are fitted around them."""
        pages = {p.page_id for p in (*self.people.values(), *self.creators.values())}
        bases = (PERSONAL_AD_ID_BASE, PERSONAL_COLLATION_ID_BASE, MARKETPLACE_ITEM_ID_BASE)
        return pages | {str(base + n) for base in bases for n in self.personal_ads.values()}


@dataclass(frozen=True)
class FictionalMap:
    """The private real -> fictional map (git-ignored, kept beside the raw run)."""

    texts: dict[str, str]
    links: dict[str, str]
    pages: dict[str, dict[str, str]]
    names: tuple[tuple[str, str], ...]
    forbidden: tuple[re.Pattern[str], ...]
    person_pages: frozenset[str]

    @classmethod
    def load(cls, path: Path) -> FictionalMap:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            texts=data["texts"], links=data["links"], pages=data["pages"],
            names=tuple(sorted(((real, fictional) for real, fictional in data["names"]), key=lambda p: -len(p[0]))),
            forbidden=tuple(re.compile(pattern, re.IGNORECASE) for pattern in data["forbidden"]),
            person_pages=frozenset(data["person_pages"]),
        )

    def replaced(self) -> set[str]:
        """The real strings this map replaces; none may survive."""
        pairs = [*self.texts.items(), *self.links.items(), *self.names]
        return {real for real, fictional in pairs if real.strip() and real != fictional}


def _templates() -> list[tuple[str, dict[str, Any]]]:
    def synthesized(reason: str) -> dict[str, Any]:
        return {"kind": "synthesized", "reason": reason, "built_by": "scripts/build_competitor_mock_fixtures.py"}

    empty_listing = "P0 recorded no empty page; keys follow the recorded first pages of this endpoint"
    return [
        ("template--empty-company-ads", {
            "provenance": synthesized(empty_listing),
            "template": {"path": "/v1/facebook/adLibrary/company/ads", "use": "empty"},
            "status": 200,
            "body": {"success": True, "credits_remaining": 0, "credits_charged": 1, "results": [],
                     "searchResultsCount": 0, "cursor": None},
        }),
        ("template--empty-search-ads", {
            "provenance": synthesized(empty_listing),
            "template": {"path": "/v1/facebook/adLibrary/search/ads", "use": "empty"},
            "status": 200,
            "body": {"success": True, "credits_remaining": 0, "credits_charged": 1, "searchResults": [],
                     "searchResultsCount": 0, "cursor": None},
        }),
        ("template--empty-search-companies", {
            "provenance": synthesized(empty_listing),
            "template": {"path": "/v1/facebook/adLibrary/search/companies", "use": "empty"},
            "status": 200,
            "body": {"success": True, "credits_remaining": 0, "credits_charged": 1, "searchResults": []},
        }),
        ("template--error-insufficient-credits", {
            "provenance": synthesized(
                "P0 never ran out of credits; 402 means out of credits, the body shape is not verified"),
            "template": {"path": "*", "use": "insufficient_credits"},
            "status": 402,
            "body": {"success": False, "message": "Out of credits. Buy more credits to continue."},
        }),
        ("template--error-ad-not-found", {
            "provenance": synthesized("P0 made no /ad call; the docs list a 404, its body is not verified"),
            "template": {"path": "/v1/facebook/adLibrary/ad", "use": "not_found"},
            "status": 404,
            "body": {"success": False, "message": "Ad not found."},
        }),
    ]


# ---------------------------------------------------------------------------
# Sanitizing
# ---------------------------------------------------------------------------


def _strip_link(url: str) -> str:
    """Drop query and fragment (tracking); an Ad Library link keeps only ``id``."""
    parts = urlsplit(url)
    query = ""
    if parts.hostname and parts.hostname.endswith("facebook.com") and parts.path.rstrip("/") == "/ads/library":
        ids = parse_qs(parts.query).get("id")
        query = urlencode({"id": ids[0]}) if ids else ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _placeholder(url: str, kind: str) -> str:
    ref = hashlib.sha256(urlsplit(url).path.encode("utf-8")).hexdigest()[:16]
    return placeholder_media_url(kind, ref, media_url_expires_at(url))


def sanitize(node: Any, key: str | None = None) -> Any:
    """Media placeholders, no avatars, no tracking parameters -- everywhere."""
    if isinstance(node, dict):
        return {k: sanitize(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [sanitize(v, key) for v in node]
    if key in AVATAR_KEYS:
        return None
    if not isinstance(node, str) or not node:
        return node
    if key in MEDIA_KEYS:
        return _placeholder(node, MEDIA_KEYS[key]) if CDN_HOST.search(node) else node
    if key in LINK_KEYS:
        return _strip_link(node)
    if key == "page_profile_uri":
        return node
    return URL_IN_TEXT.sub(lambda match: _strip_link(match.group(0)), node)


def _replace_strings(node: Any, replacements: dict[str, str]) -> Any:
    text = json.dumps(node, ensure_ascii=False)
    for old, new in sorted(replacements.items(), key=lambda item: -len(item[0])):
        text = text.replace(old, new)
    return json.loads(text)


def _profile_segment(uri: Any) -> str | None:
    """The last path segment of a profile link (vanity alias, or ``Name-Name-<id>``)."""
    if not isinstance(uri, str):
        return None
    segments = [segment for segment in urlsplit(uri).path.split("/") if segment and segment != "_u"]
    return segments[-1] if segments else None


def _page_replacements(node: dict[str, Any], pseudonym: Pseudonym, names: Pseudonyms) -> dict[str, str]:
    real_id, real_name = str(node["page_id"]), node.get("page_name")
    replacements = {real_id: pseudonym.page_id}
    if isinstance(real_name, str) and " " in real_name.strip():
        replacements[real_name] = pseudonym.name
        replacements[real_name.replace(" ", "-")] = pseudonym.name.replace(" ", "-")
    elif isinstance(real_name, str) and real_name.strip():
        # A handle-like name is also the profile link's last segment.
        replacements[real_name] = pseudonym.alias
    segment = _profile_segment(node.get("page_profile_uri"))
    if segment and segment not in replacements and not segment.endswith(real_id):
        replacements[segment] = pseudonym.alias
    names.scrub(*replacements)
    return replacements


def _body_lines(text: Any) -> list[str]:
    if not isinstance(text, str):
        return []
    return [line.strip() for line in text.splitlines() if len(line.strip()) >= MIN_AUDITED_LINE]


def _pseudonymize_personal_ad(ad: dict[str, Any], names: Pseudonyms) -> dict[str, Any]:
    """A person selling on Marketplace: new identity, ids and a neutral body."""
    snapshot = ad["snapshot"]
    reshared = snapshot.get("root_reshared_post") if isinstance(snapshot.get("root_reshared_post"), dict) else {}
    n = names.personal_ad(ad["ad_archive_id"])
    replacements: dict[str, str] = {}
    for node in (snapshot, reshared.get("page")):
        if isinstance(node, dict) and node.get("page_id"):
            # The advertiser comes first: a reshared page with the same name keeps its pseudonym.
            for real, pseudonym in _page_replacements(node, names.person(str(node["page_id"])), names).items():
                replacements.setdefault(real, pseudonym)
    title = snapshot.get("title") or ""
    for text in (snapshot.get("body"), (reshared.get("text") or {}).get("body")):
        lines = _body_lines(text.get("text") if isinstance(text, dict) else text)
        names.scrub(*(line for line in lines if line not in title))
    names.scrub(ad["ad_archive_id"], ad.get("collation_id"), *re.findall(r"\d{8,}", snapshot.get("link_url") or ""))

    ad = _replace_strings(ad, replacements)
    ad["ad_archive_id"] = str(PERSONAL_AD_ID_BASE + n)
    if ad.get("collation_id"):
        ad["collation_id"] = str(PERSONAL_COLLATION_ID_BASE + n)
    ad["url"] = f"https://www.facebook.com/ads/library?id={ad['ad_archive_id']}"
    snapshot = ad["snapshot"]
    title = snapshot.get("title")
    note = f"Private Marketplace listing: {title}. Message the seller for details." if title else (
        "Private Marketplace listing. Message the seller for details.")
    snapshot["body"] = {"text": note}
    snapshot["link_url"] = f"https://facebook.com/marketplace/item/{MARKETPLACE_ITEM_ID_BASE + n}/"
    if isinstance(snapshot.get("root_reshared_post"), dict):
        snapshot["root_reshared_post"]["text"] = {"body": {"text": note}}
    return ad


def _pseudonymize_creators(ad: dict[str, Any], names: Pseudonyms) -> dict[str, Any]:
    """Partnership ads name a creator page next to the advertiser; rename it."""
    snapshot = ad.get("snapshot") or {}
    advertiser = str(ad.get("page_id"))
    replacements: dict[str, str] = {}
    for node in (snapshot, snapshot.get("branded_content")):
        if isinstance(node, dict) and node.get("page_id") is not None and str(node["page_id"]) != advertiser:
            replacements.update(_page_replacements(node, names.creator(str(node["page_id"])), names))
    return _replace_strings(ad, replacements) if replacements else ad


def _pseudonymize_candidate(candidate: dict[str, Any], names: Pseudonyms, person_pages: frozenset[str]) -> dict[str, Any]:
    if str(candidate.get("page_id")) not in person_pages:
        return candidate
    pseudonym = names.person(str(candidate["page_id"]))
    names.scrub(candidate["page_id"], candidate.get("name"), candidate.get("page_alias"), candidate.get("ig_username"))
    candidate = dict(candidate)
    candidate["page_id"] = pseudonym.page_id
    candidate["name"] = pseudonym.name
    for key in ("page_alias", "ig_username"):
        if candidate.get(key):
            candidate[key] = pseudonym.alias
    return candidate


def _sanitize_item(item: Any, names: Pseudonyms, person_pages: frozenset[str]) -> Any:
    if not isinstance(item, dict):
        return item
    if "snapshot" not in item:
        return _pseudonymize_candidate(item, names, person_pages)
    snapshot = item["snapshot"] or {}
    # The advertiser's own page is a personal profile (a creator's is handled below).
    if str(snapshot.get("page_id")) == str(item.get("page_id")) and snapshot.get("page_categories") == [PERSONAL_PAGE_CATEGORY]:
        return _pseudonymize_personal_ad(item, names)
    return _pseudonymize_creators(item, names)


def _sanitize_body(
    body: dict[str, Any], cursors: dict[str, str], fixture_name: str, names: Pseudonyms, fictional: FictionalMap,
) -> dict[str, Any]:
    body = sanitize(body)
    for key in ("results", "searchResults"):
        if isinstance(body.get(key), list):
            body[key] = [
                _fictional_item(_sanitize_item(item, names, fictional.person_pages), fictional, names, fixture_name)
                for item in body[key]
            ]
    if isinstance(body.get("cursor"), str) and body["cursor"]:
        stem, _, page = fixture_name.rpartition("--p")
        token = f"mock-cursor--{stem.split('--', 1)[1]}--p{int(page) + 1}"
        cursors[body["cursor"]] = token
        body["cursor"] = token
    return body


# ---------------------------------------------------------------------------
# Fictional brands (#428)
# ---------------------------------------------------------------------------


def _fictional_copy(node: Any, fictional: FictionalMap, where: str, key: str | None = None) -> Any:
    """Ad copy and landing links replaced whole; a value the map lacks fails the build."""
    if isinstance(node, dict):
        return {k: _fictional_copy(v, fictional, where, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_fictional_copy(v, fictional, where, key) for v in node]
    if not isinstance(node, str) or not node:
        return node
    if key in COPY_KEYS:
        if node not in fictional.texts:
            raise SystemExit(f"{where}: no fictional copy for a {key!r} value ({len(node)} chars)")
        return fictional.texts[node]
    if key in LINK_KEYS and urlsplit(node).hostname not in PLATFORM_HOSTS:
        if node not in fictional.links:
            raise SystemExit(f"{where}: no fictional link for a {key!r} value")
        return fictional.links[node]
    return node


def _fictional_item(item: Any, fictional: FictionalMap, names: Pseudonyms, where: str) -> Any:
    """An ad gets fictional copy and links; a page-search candidate its fictional name fields."""
    if not isinstance(item, dict):
        return item
    if "snapshot" in item:
        return _fictional_copy(item, fictional, where)
    fields = fictional.pages.get(str(item.get("page_id")), {})
    # A short alias ("brand") also spells part of a common word; the map's forbidden patterns cover it.
    names.scrub(*(item.get(field) for field in fields
                  if field == "name" or len(str(item.get(field) or "")) >= MIN_AUDITED_ALIAS))
    return {**item, **{field: value for field, value in fields.items() if field in CANDIDATE_FIELDS}}


def _collect_ids(node: Any, key: str | None = None, found: set[str] | None = None) -> set[str]:
    """Page, ad and collation ids, and the long numbers in Meta links (profile and page ids)."""
    found = set() if found is None else found
    if isinstance(node, dict):
        for k, v in node.items():
            _collect_ids(v, k, found)
    elif isinstance(node, list):
        for v in node:
            _collect_ids(v, key, found)
    elif isinstance(node, str):
        if key in ID_KEYS and node.isdigit():
            found.add(node)
        elif key in PROFILE_KEYS and urlsplit(node).hostname in PLATFORM_HOSTS:
            found.update(LONG_NUMBER.findall(node))
    return found


def _successor(previous: str, length: int) -> str:
    """A ``length``-digit string just after ``previous`` in string order."""
    if not previous:
        return "1" + "0" * (length - 2) + "1"
    if len(previous) < length:
        return previous + "0" * (length - len(previous) - 1) + "1"
    return str(int(previous[:length]) + 1)


def fictional_ids(real: set[str], synthetic: set[str]) -> dict[str, str]:
    """Real id -> a made-up id of the same length and the same string order
    (ids are sorted as strings downstream), fitted around the ``synthetic`` ones."""
    mapping: dict[str, str] = {}
    previous = ""
    for value in sorted(real | synthetic):
        if value in synthetic:
            previous = value
            continue
        mapping[value] = previous = _successor(previous, len(value))
    renamed = [mapping.get(value, value) for value in sorted(real | synthetic)]
    if renamed != sorted(renamed) or len(set(renamed)) != len(renamed) or any(
            len(mapping[value]) != len(value) for value in mapping):
        raise SystemExit("the fictional ids would not keep the ids' length and order")
    return mapping


def _fictional_text(text: str, fictional: FictionalMap, ids: dict[str, str]) -> str:
    """Advertiser names, profile links and queries by substring, then the ids."""
    for real, name in fictional.names:
        text = text.replace(real, name)
    return LONG_NUMBER.sub(lambda match: ids.get(match.group(0), match.group(0)), text)


def _audit(text: str, name: str, scrubbed: set[str], forbidden: tuple[re.Pattern[str], ...] = ()) -> None:
    problems = [
        f"a scrubbed string ({len(real)} chars)" for real in scrubbed
        if real in text or json.dumps(real, ensure_ascii=False)[1:-1] in text
    ]
    problems.extend(f"forbidden pattern #{index}" for index, pattern in enumerate(forbidden) if pattern.search(text))
    if CDN_HOST.search(text):
        problems.append("a signed CDN link")
    if re.search(r'"(?:%s)":\s*"' % "|".join(sorted(AVATAR_KEYS)), text):
        problems.append("an avatar URL")
    if re.search(r"x-api-key|api[_-]?key|creditCount", text, re.IGNORECASE):
        problems.append("an API key field")
    for url in URL_IN_TEXT.findall(text):
        parts = urlsplit(url)
        if parts.query and parts.hostname != MOCK_MEDIA_HOST and parts.path.rstrip("/") != "/ads/library":
            problems.append(f"a query string on {parts.hostname}")
    if problems:
        raise SystemExit(f"{name}: output still contains " + ", ".join(sorted(set(problems))))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _latencies(raw_dir: Path) -> dict[int, int]:
    calls = raw_dir.parent / "calls.jsonl"
    records = (json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines() if line.strip())
    return {record["seq"]: record["latency_ms"] for record in records}


def _write_placeholders(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, colour in (("placeholder-image.png", (217, 217, 217)), ("placeholder-video-poster.png", (140, 140, 140))):
        Image.new("RGB", (600, 600), colour).save(directory / name, format="PNG", optimize=True)
    # The page searches' avatar links (answered by the mock itself, #332).
    Image.new("RGB", (200, 200), (99, 102, 241)).save(directory / "placeholder-avatar.png", format="PNG", optimize=True)


def _raw_file(raw_dir: Path, seq: str) -> Path:
    matches = sorted(raw_dir.glob(f"{seq}-*.json"))
    if len(matches) != 1:
        raise SystemExit(f"expected one raw file for call {seq}, found {len(matches)}")
    return matches[0]


def build(raw_dir: Path, out_dir: Path, fictional: FictionalMap) -> list[str]:
    latencies = _latencies(raw_dir)
    names = Pseudonyms()
    cursors: dict[str, str] = {}
    outputs: list[tuple[str, dict[str, Any]]] = []
    for seq, fixture_name in RAW_CALLS:
        raw = json.loads(_raw_file(raw_dir, seq).read_text(encoding="utf-8"))
        if raw["status"] != 200:
            raise SystemExit(f"call {seq}: expected a recorded 200, got {raw['status']}")
        params = {key: value for key, value in raw["request"]["params"].items() if value not in (None, "")}
        if "cursor" in params:
            params["cursor"] = cursors[params["cursor"]]
        outputs.append((fixture_name, {
            "provenance": {
                "kind": "recorded",
                "run": RUN_ID,
                "raw_file": f"{seq}-*.json",
                "captured_at": CAPTURED_AT.isoformat(),
                "latency_ms": latencies[int(seq)],
                "sanitized_by": "scripts/build_competitor_mock_fixtures.py",
            },
            "request": {"path": raw["request"]["path"], "params": params},
            "status": raw["status"],
            "body": _sanitize_body(raw["body"], cursors, fixture_name, names, fictional),
        }))
    synthetic = names.synthetic_ids()
    ids = fictional_ids(_collect_ids([fixture for _, fixture in outputs]) - synthetic, synthetic)
    names.scrub(*ids)
    outputs = [
        (name, json.loads(_fictional_text(json.dumps(fixture, ensure_ascii=False), fictional, ids)))
        for name, fixture in outputs
    ]
    outputs.extend(_templates())

    scrubbed = names.scrubbed | fictional.replaced()
    texts = {name: json.dumps(fixture, ensure_ascii=False, indent=1) + "\n" for name, fixture in outputs}
    for name, text in texts.items():
        _audit(text, name, scrubbed, fictional.forbidden)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    _write_placeholders(out_dir / "media")
    for name, text in texts.items():
        (out_dir / f"{name}.json").write_text(text, encoding="utf-8")
    return list(texts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--raw-dir", type=Path, required=True, help="the P0 run directory or its raw/ directory")
    parser.add_argument("--fictional-map", type=Path, required=True,
                        help="the private real -> fictional map (JSON: texts, links, pages, names, forbidden, "
                             "person_pages)")
    parser.add_argument("--out-dir", type=Path, default=FIXTURE_DIR)
    args = parser.parse_args(argv)
    raw_dir = args.raw_dir if args.raw_dir.name == "raw" else args.raw_dir / "raw"
    for name in build(raw_dir, args.out_dir, FictionalMap.load(args.fictional_map)):
        print(f"wrote {name}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
