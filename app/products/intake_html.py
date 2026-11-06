"""Parse a product page into what extraction needs (Issue #161).

Standard library only (``html.parser``): a page is untrusted input, and the
parser never executes, fetches or resolves anything -- it reads tags and
text. Extracted:

* JSON-LD ``Product`` / ``ProductGroup`` nodes (also inside ``@graph`` and
  lists): name, description, brand, model, sku, color, material,
  ``additionalProperty`` specs, variant names and images. ``offers`` (price,
  currency, availability) are deliberately dropped: commercial truth belongs
  to OfferVersion, never to the product card (creative-library contract §5).
* OpenGraph / ``product:`` meta tags, ``<title>``, meta description.
* Main text, without script/style/navigation/footer chrome, collapsed.
* Image candidates in priority order -- ``og:image``, JSON-LD images,
  ``<link rel=image_src>``, then ``<img>`` (``src``/lazy ``data-*``/the
  largest ``srcset`` entry) -- skipping data URIs, SVG/GIF, declared
  thumbnails under 150 px and obvious icons/logos/tracking pixels.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_MAIN_TEXT_CHARS = 20_000
MAX_IMAGE_CANDIDATES = 40
MIN_DECLARED_IMAGE_SIDE = 150

_SKIP_TEXT_TAGS = frozenset({
    "script", "style", "noscript", "svg", "template", "iframe", "nav", "footer",
    "header", "form", "select", "option", "button", "canvas", "object",
})
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "li", "ul", "ol", "tr", "td", "th", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "br", "dd", "dt", "dl", "main", "aside",
    "figcaption", "blockquote", "pre",
})
_IMAGE_NOISE = re.compile(
    r"(sprite|icon|logo|favicon|pixel|tracking|spacer|blank|avatar|badge|payment|flag|loader|placeholder)",
    re.IGNORECASE,
)
_LAZY_SRC_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-zoom-image", "data-large_image", "src")
_DROPPED_JSONLD_KEYS = frozenset({"offers", "price", "priceCurrency", "priceSpecification", "aggregateOffer"})


@dataclass(frozen=True)
class ImageCandidateRef:
    url: str
    source: str  # og | json_ld | link | img
    alt: str = ""


@dataclass
class ParsedPage:
    title: str = ""
    meta_description: str = ""
    og: dict[str, list[str]] = field(default_factory=dict)
    json_ld_products: list[dict] = field(default_factory=list)
    main_text: str = ""
    images: list[ImageCandidateRef] = field(default_factory=list)
    canonical_url: str = ""

    @property
    def has_product_signal(self) -> bool:
        og_type = " ".join(self.og.get("og:type", [])).lower()
        return bool(self.json_ld_products) or "product" in og_type or any(
            key.startswith("product:") for key in self.og
        )

    def summary(self) -> dict:
        """Bounded, digest-friendly evidence of what was parsed (no page body)."""
        return {
            "title": self.title[:300],
            "meta_description": self.meta_description[:500],
            "og_type": (self.og.get("og:type") or [""])[0][:100],
            "json_ld_product_count": len(self.json_ld_products),
            "json_ld_product_names": [str(p.get("name") or "")[:200] for p in self.json_ld_products[:5]],
            "main_text_chars": len(self.main_text),
            "image_candidate_count": len(self.images),
            "has_product_signal": self.has_product_signal,
            "canonical_url": self.canonical_url[:2048],
        }


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, list[str]] = {}
        self.json_ld_blocks: list[str] = []
        self.text_parts: list[str] = []
        self.img: list[tuple[str, str, dict[str, str]]] = []  # (tag, kind, attrs)
        self.base_href = ""
        self.canonical = ""
        self._skip_depth = 0
        self._in_title = False
        self._in_json_ld = False
        self._json_ld_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = {name.lower(): (value or "") for name, value in attrs}
        if tag == "base" and attributes.get("href") and not self.base_href:
            self.base_href = attributes["href"]
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").strip().lower()
            content = attributes.get("content", "").strip()
            if key and content:
                self.meta.setdefault(key, []).append(content)
        elif tag == "link":
            rel = attributes.get("rel", "").lower()
            if "canonical" in rel and attributes.get("href") and not self.canonical:
                self.canonical = attributes["href"]
            if "image_src" in rel and attributes.get("href"):
                self.img.append(("link", "link", {"src": attributes["href"]}))
        elif tag == "img":
            self.img.append(("img", "img", attributes))
        elif tag == "source" and attributes.get("srcset"):
            self.img.append(("source", "img", attributes))
        if tag == "script":
            if "ld+json" in attributes.get("type", "").lower():
                self._in_json_ld = True
                self._json_ld_buffer = []
            self._skip_depth += 1
            return
        if tag in _SKIP_TEXT_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag in _SKIP_TEXT_TAGS or tag == "script":
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script":
            if self._in_json_ld:
                self.json_ld_blocks.append("".join(self._json_ld_buffer))
                self._in_json_ld = False
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _SKIP_TEXT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_ld_buffer.append(data)
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth == 0 and data.strip():
            self.text_parts.append(data)


def _collapse(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _absolute(url: str, base: str) -> str:
    candidate = (url or "").strip()
    if not candidate or candidate.startswith("data:") or candidate.startswith("javascript:"):
        return ""
    joined = urljoin(base, candidate)
    parts = urlsplit(joined)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _largest_srcset(srcset: str) -> str:
    best, best_width = "", -1
    for entry in srcset.split(","):
        bits = entry.strip().split()
        if not bits:
            continue
        width = 0
        if len(bits) > 1:
            descriptor = bits[1].lower()
            try:
                if descriptor.endswith("w"):
                    width = int(float(descriptor[:-1]))
                elif descriptor.endswith("x"):
                    width = int(float(descriptor[:-1]) * 1000)
            except ValueError:
                width = 0
        if width > best_width:
            best, best_width = bits[0], width
    return best


def _declared_side_too_small(attributes: dict[str, str]) -> bool:
    for key in ("width", "height"):
        value = attributes.get(key, "").strip().lower().removesuffix("px")
        if value.isdigit() and int(value) < MIN_DECLARED_IMAGE_SIDE:
            return True
    return False


def _iter_json_ld_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _iter_json_ld_nodes(item)
    elif isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if graph is not None:
            yield from _iter_json_ld_nodes(graph)


def _types(node: dict) -> set[str]:
    declared = node.get("@type")
    if isinstance(declared, str):
        return {declared.split("/")[-1].lower()}
    if isinstance(declared, list):
        return {str(item).split("/")[-1].lower() for item in declared}
    return set()


def _text(value, limit: int = 2000) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value") or ""
    if isinstance(value, list):
        value = ", ".join(_text(item, limit) for item in value if item)
    return " ".join(str(value or "").split())[:limit]


def _image_urls(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        url = value.get("contentUrl") or value.get("url")
        return [url] if isinstance(url, str) else []
    if isinstance(value, list):
        urls: list[str] = []
        for item in value:
            urls.extend(_image_urls(item))
        return urls
    return []


def _product_from_json_ld(node: dict) -> dict:
    product: dict = {}
    for key in ("name", "description", "model", "sku", "mpn", "gtin13", "color", "material", "category"):
        if node.get(key):
            product[key] = _text(node[key])
    if node.get("brand"):
        product["brand"] = _text(node["brand"], 200)
    specs: dict[str, str] = {}
    properties = node.get("additionalProperty")
    for prop in properties if isinstance(properties, list) else [properties] if isinstance(properties, dict) else []:
        if isinstance(prop, dict) and prop.get("name") and prop.get("value") is not None:
            specs[_text(prop["name"], 100)] = _text(prop["value"], 500)
    if specs:
        product["specs"] = specs
    variants = node.get("hasVariant")
    if isinstance(variants, list):
        names = []
        for variant in variants:
            if isinstance(variant, dict):
                names.append({
                    key: _text(variant[key], 300)
                    for key in ("name", "sku", "color", "size", "model")
                    if variant.get(key) and key not in _DROPPED_JSONLD_KEYS
                })
        product["variants"] = [name for name in names if name][:20]
    images = _image_urls(node.get("image"))
    if images:
        product["images"] = images[:20]
    return product


def parse_product_page(html: str, base_url: str) -> ParsedPage:
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 -- a malformed page still yields what was read
        pass
    base = urljoin(base_url, parser.base_href) if parser.base_href else base_url
    page = ParsedPage()
    page.title = " ".join("".join(parser.title_parts).split())[:500]
    page.og = {key: values for key, values in parser.meta.items() if key.startswith(("og:", "product:"))}
    page.meta_description = (parser.meta.get("description") or [""])[0][:1000]
    page.canonical_url = _absolute(parser.canonical, base) if parser.canonical else ""
    page.main_text = _collapse("".join(parser.text_parts))[:MAX_MAIN_TEXT_CHARS]

    products: list[dict] = []
    for block in parser.json_ld_blocks:
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for node in _iter_json_ld_nodes(data):
            if _types(node) & {"product", "productgroup", "individualproduct", "productmodel"}:
                products.append(_product_from_json_ld(node))
    page.json_ld_products = products[:5]

    ordered: list[ImageCandidateRef] = []
    for url in page.og.get("og:image", []) + page.og.get("og:image:url", []) + page.og.get("og:image:secure_url", []):
        ordered.append(ImageCandidateRef(url=url, source="og"))
    for product in page.json_ld_products:
        for url in product.get("images", []):
            ordered.append(ImageCandidateRef(url=url, source="json_ld"))
    for tag, kind, attributes in parser.img:
        if kind == "link":
            ordered.append(ImageCandidateRef(url=attributes.get("src", ""), source="link"))
            continue
        if _declared_side_too_small(attributes):
            continue
        url = ""
        if attributes.get("srcset"):
            url = _largest_srcset(attributes["srcset"])
        if not url and attributes.get("data-srcset"):
            url = _largest_srcset(attributes["data-srcset"])
        if not url:
            url = next((attributes[name] for name in _LAZY_SRC_ATTRS if attributes.get(name)), "")
        alt = attributes.get("alt", "")
        if _IMAGE_NOISE.search(url) or _IMAGE_NOISE.search(attributes.get("class", "")):
            continue
        ordered.append(ImageCandidateRef(url=url, source="img", alt=" ".join(alt.split())[:200]))

    seen: set[str] = set()
    for candidate in ordered:
        absolute = _absolute(candidate.url, base)
        if not absolute or absolute in seen:
            continue
        path = urlsplit(absolute).path.lower()
        if path.endswith((".svg", ".gif", ".ico")):
            continue
        seen.add(absolute)
        page.images.append(ImageCandidateRef(url=absolute, source=candidate.source, alt=candidate.alt))
        if len(page.images) >= MAX_IMAGE_CANDIDATES:
            break
    return page


def page_digest_text(page: ParsedPage, *, max_chars: int) -> str:
    """The page as extraction reads it: structured facts first, then body text."""
    sections: list[str] = []
    if page.title:
        sections.append(f"Page title: {page.title}")
    if page.meta_description:
        sections.append(f"Meta description: {page.meta_description}")
    og_lines = [
        f"{key}: {' | '.join(values)[:500]}"
        for key, values in page.og.items()
        if not key.startswith(("og:image", "product:price", "og:price"))
        and "price" not in key and "currency" not in key
    ]
    if og_lines:
        sections.append("OpenGraph:\n" + "\n".join(og_lines[:30]))
    for index, product in enumerate(page.json_ld_products, start=1):
        shown = {key: value for key, value in product.items() if key != "images"}
        sections.append(
            f"JSON-LD Product {index}:\n" + json.dumps(shown, ensure_ascii=False, sort_keys=True)[:4000]
        )
    header = "\n\n".join(sections)
    remaining = max(0, max_chars - len(header) - 20)
    if page.main_text and remaining > 0:
        header = f"{header}\n\nPage text:\n{page.main_text[:remaining]}"
    return header[:max_chars]
