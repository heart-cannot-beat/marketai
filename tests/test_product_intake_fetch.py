"""Issue #161 -- the SSRF-safe intake fetcher, the page parser and the card rules.

No network: DNS is a fake resolver and HTTP an ``httpx.MockTransport``.

* Address policy: private, loopback, link-local, cloud metadata
  (169.254.169.254, fd00:ec2::254), CGNAT, IPv6 loopback/ULA and IPv4
  embedded in IPv6 (mapped, NAT64, 6to4) are refused; public addresses pass.
* URL policy: only http(s), ports 80/443, no userinfo, no ``localhost``.
* Every hop is checked: a redirect to a private host, DNS answering a
  private (or mixed) address and too many redirects are refused, with the
  *category* of the blocked address only.
* Pinning: the request goes to the address that was checked (URL carries the
  IP, ``Host``/SNI carry the name) and the name is resolved once per hop, so
  a rebinding resolver cannot swap the address after the check.
* Bounds: 10 s deadline, 5 MB page (truncated), 10 MB image (refused),
  content-type and magic-byte checks, fixed User-Agent.
* The HTML parser (JSON-LD Product, OpenGraph, images, offers dropped) and
  the extraction normalization (commercial specs dropped, unusable answers
  refused) plus the new ProductVersion content rules.
"""
from __future__ import annotations

import asyncio
import io
import json
import unittest

import httpx
from PIL import Image

from app.library.contracts import ENTRY_PRODUCT, validate_content
from app.products import intake_extraction as extraction
from app.products.intake_fetch import (
    MAX_PAGE_BYTES,
    USER_AGENT,
    FetchRefused,
    SafeFetcher,
    blocked_category,
    parse_target,
)
from app.products.intake_html import page_digest_text, parse_product_page

PUBLIC_IP = "93.184.216.34"


def _png(size=(400, 400), colour=(10, 120, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _resolver(mapping: dict[str, list[str]], calls: list[str] | None = None):
    async def resolve(host: str, port: int) -> list[str]:
        if calls is not None:
            calls.append(host)
        if host not in mapping:
            raise OSError("NXDOMAIN")
        return list(mapping[host])
    return resolve


class _Site:
    """A fake web: ``(host header, path) -> response``; records every request."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.headers.get("host", ""), request.url.path)
        response = self.routes.get(key)
        if response is None:
            return httpx.Response(404, text="missing")
        return response

    def fetcher(self, mapping: dict[str, list[str]], **kwargs) -> SafeFetcher:
        return SafeFetcher(resolver=_resolver(mapping), transport=httpx.MockTransport(self), **kwargs)


def _html(body: str = "<html><head><title>Charger</title></head><body><p>A product</p></body></html>"):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=body)


class TestAddressPolicy(unittest.TestCase):
    def test_refused_categories(self):
        cases = {
            "10.1.2.3": "private",
            "172.16.0.1": "private",
            "192.168.1.1": "private",
            "127.0.0.1": "loopback",
            "127.8.8.8": "loopback",
            "169.254.169.254": "metadata",
            "169.254.10.10": "link_local",
            "100.100.100.200": "metadata",
            "100.64.0.1": "cgnat",
            "0.0.0.0": "unspecified",
            "224.0.0.1": "multicast",
            "::1": "loopback",
            "fe80::1": "link_local",
            "fd12:3456::1": "private",
            "fd00:ec2::254": "metadata",
            "::ffff:127.0.0.1": "loopback",
            "::ffff:169.254.169.254": "metadata",
            "64:ff9b::a00:1": "private",
            "2002:a00:1::": "private",
            "::": "unspecified",
        }
        for address, category in cases.items():
            with self.subTest(address=address):
                self.assertEqual(blocked_category(address), category)

    def test_public_addresses_pass(self):
        for address in (PUBLIC_IP, "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"):
            with self.subTest(address=address):
                self.assertIsNone(blocked_category(address))

    def test_url_policy(self):
        refused = {
            "ftp://shop.example/x": "scheme_not_allowed",
            "file:///etc/passwd": "scheme_not_allowed",
            "gopher://shop.example/": "scheme_not_allowed",
            "http://shop.example:8080/": "port_not_allowed",
            "https://shop.example:22/": "port_not_allowed",
            "https://user:pw@shop.example/": "credentials_in_url",
            "http://localhost/admin": "blocked_address",
            "http://api.localhost/": "blocked_address",
            "http://metadata.google.internal/computeMetadata/v1/": "blocked_address",
            "": "invalid_url",
            "https://" + "a" * 3000: "invalid_url",
        }
        for url, code in refused.items():
            with self.subTest(url=url[:40]):
                with self.assertRaises(FetchRefused) as caught:
                    parse_target(url)
                self.assertEqual(caught.exception.code, code)
        target = parse_target("HTTPS://Shop.Example/p/1?x=2#frag")
        self.assertEqual((target.scheme, target.host, target.port), ("https", "shop.example", 443))
        self.assertEqual(target.path_query, "/p/1?x=2")


class TestSafeFetcher(unittest.TestCase):
    def _fetch(self, fetcher: SafeFetcher, url: str, *, image: bool = False):
        return asyncio.run(fetcher.fetch_image(url) if image else fetcher.fetch_page(url))

    def test_request_is_pinned_to_the_checked_address(self):
        site = _Site({("shop.example", "/p/charger"): _html()})
        calls: list[str] = []
        # A rebinding resolver: public on the first answer, loopback after.
        answers = iter([[PUBLIC_IP], ["127.0.0.1"], ["127.0.0.1"]])

        async def rebinding(host: str, port: int) -> list[str]:
            calls.append(host)
            return next(answers)

        fetcher = SafeFetcher(resolver=rebinding, transport=httpx.MockTransport(site))
        result = self._fetch(fetcher, "https://shop.example/p/charger")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls, ["shop.example"], "resolved exactly once for the one hop")
        request = site.requests[0]
        self.assertEqual(request.url.host, PUBLIC_IP)
        self.assertEqual(request.headers["host"], "shop.example")
        self.assertEqual(request.extensions.get("sni_hostname"), "shop.example")
        self.assertEqual(request.headers["user-agent"], USER_AGENT)

    def test_ipv6_public_address_is_bracketed(self):
        site = _Site({("shop6.example", "/"): _html()})
        fetcher = site.fetcher({"shop6.example": ["2606:4700:4700::1111"]})
        self._fetch(fetcher, "http://shop6.example/")
        self.assertEqual(site.requests[0].url.host, "2606:4700:4700::1111")

    def test_private_loopback_metadata_and_ipv6_targets_are_refused(self):
        site = _Site({})
        cases = {
            "http://10.0.0.5/": "private",
            "http://127.0.0.1/": "loopback",
            "http://169.254.169.254/latest/meta-data/": "metadata",
            "http://[::1]/": "loopback",
            "http://[fd00::1]/": "private",
            "http://[::ffff:10.0.0.1]/": "private",
        }
        fetcher = site.fetcher({})
        for url, category in cases.items():
            with self.subTest(url=url):
                with self.assertRaises(FetchRefused) as caught:
                    self._fetch(fetcher, url)
                self.assertEqual(caught.exception.code, "blocked_address")
                self.assertEqual(caught.exception.category, category)
                self.assertNotIn("10.0.0.5", caught.exception.detail)
        self.assertEqual(site.requests, [], "nothing was sent to a refused address")

    def test_dns_answering_a_private_address_is_refused(self):
        site = _Site({})
        for answer, category in ((["192.168.0.10"], "private"), ([PUBLIC_IP, "127.0.0.1"], "loopback"),
                                 (["169.254.169.254"], "metadata")):
            with self.subTest(answer=answer):
                fetcher = site.fetcher({"evil.example": answer})
                with self.assertRaises(FetchRefused) as caught:
                    self._fetch(fetcher, "https://evil.example/")
                self.assertEqual((caught.exception.code, caught.exception.category), ("blocked_address", category))
        self.assertEqual(site.requests, [])

    def test_redirect_to_a_private_host_is_refused_at_that_hop(self):
        site = _Site({
            ("shop.example", "/go"): httpx.Response(302, headers={"location": "http://internal.example/admin"}),
        })
        fetcher = site.fetcher({"shop.example": [PUBLIC_IP], "internal.example": ["10.0.0.8"]})
        with self.assertRaises(FetchRefused) as caught:
            self._fetch(fetcher, "https://shop.example/go")
        self.assertEqual((caught.exception.code, caught.exception.category), ("blocked_address", "private"))
        self.assertEqual(len(site.requests), 1, "the private hop was never requested")

    def test_redirect_to_a_metadata_literal_is_refused(self):
        site = _Site({
            ("shop.example", "/go"): httpx.Response(
                301, headers={"location": "http://169.254.169.254/latest/meta-data/iam"},
            ),
        })
        fetcher = site.fetcher({"shop.example": [PUBLIC_IP]})
        with self.assertRaises(FetchRefused) as caught:
            self._fetch(fetcher, "https://shop.example/go")
        self.assertEqual(caught.exception.category, "metadata")

    def test_redirect_to_another_scheme_is_refused(self):
        site = _Site({("shop.example", "/go"): httpx.Response(302, headers={"location": "file:///etc/passwd"})})
        with self.assertRaises(FetchRefused) as caught:
            self._fetch(site.fetcher({"shop.example": [PUBLIC_IP]}), "https://shop.example/go")
        self.assertEqual(caught.exception.code, "scheme_not_allowed")

    def test_three_redirects_are_followed_and_a_fourth_is_refused(self):
        routes = {
            ("shop.example", f"/r{index}"): httpx.Response(302, headers={"location": f"/r{index + 1}"})
            for index in range(4)
        }
        routes[("shop.example", "/r3")] = _html()
        site = _Site(routes)
        result = self._fetch(site.fetcher({"shop.example": [PUBLIC_IP]}), "https://shop.example/r0")
        self.assertEqual(result.final_url, "https://shop.example/r3")
        self.assertEqual(len(result.hops), 4)

        routes[("shop.example", "/r3")] = httpx.Response(302, headers={"location": "/r4"})
        routes[("shop.example", "/r4")] = _html()
        with self.assertRaises(FetchRefused) as caught:
            self._fetch(site.fetcher({"shop.example": [PUBLIC_IP]}), "https://shop.example/r0")
        self.assertEqual(caught.exception.code, "too_many_redirects")

    def test_status_codes_map_to_reason_codes(self):
        cases = {403: ("access_denied", False), 401: ("access_denied", False), 429: ("access_denied", True),
                 404: ("not_found", False), 500: ("http_error", True), 418: ("http_error", False)}
        for status, (code, retryable) in cases.items():
            with self.subTest(status=status):
                site = _Site({("shop.example", "/"): httpx.Response(status)})
                with self.assertRaises(FetchRefused) as caught:
                    self._fetch(site.fetcher({"shop.example": [PUBLIC_IP]}), "https://shop.example/")
                self.assertEqual((caught.exception.code, caught.exception.retryable), (code, retryable))

    def test_timeout_is_bounded_and_retryable(self):
        async def slow(request):
            await asyncio.sleep(2)
            return _html()

        fetcher = SafeFetcher(
            resolver=_resolver({"slow.example": [PUBLIC_IP]}),
            transport=httpx.MockTransport(slow), page_timeout=0.05,
        )
        with self.assertRaises(FetchRefused) as caught:
            self._fetch(fetcher, "https://slow.example/")
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("timeout", True))

    def test_unresolvable_host(self):
        with self.assertRaises(FetchRefused) as caught:
            self._fetch(_Site({}).fetcher({}), "https://nowhere.example/")
        self.assertEqual(caught.exception.code, "dns_failed")

    def test_page_body_is_cut_at_5_mb(self):
        big = "<html><body>" + "x" * (MAX_PAGE_BYTES + 1000) + "</body></html>"
        site = _Site({("shop.example", "/"): _html(big)})
        result = self._fetch(site.fetcher({"shop.example": [PUBLIC_IP]}), "https://shop.example/")
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.body), MAX_PAGE_BYTES)

    def test_content_type_checks(self):
        site = _Site({
            ("shop.example", "/data.json"): httpx.Response(200, headers={"content-type": "application/json"}, text="{}"),
            ("shop.example", "/fake.png"): httpx.Response(200, headers={"content-type": "image/png"}, content=b"<html>"),
            ("shop.example", "/real.png"): httpx.Response(200, headers={"content-type": "image/png"}, content=_png()),
            ("shop.example", "/page"): _html(),
        })
        fetcher = site.fetcher({"shop.example": [PUBLIC_IP]})
        for path in ("/data.json", "/fake.png"):
            with self.subTest(path=path):
                with self.assertRaises(FetchRefused) as caught:
                    self._fetch(fetcher, f"https://shop.example{path}")
                self.assertEqual(caught.exception.code, "unsupported_content_type")
        self.assertEqual(self._fetch(fetcher, "https://shop.example/real.png").kind, "image")
        with self.assertRaises(FetchRefused):
            self._fetch(fetcher, "https://shop.example/page", image=True)

    def test_images_over_10_mb_are_refused(self):
        huge = b"\x89PNG\r\n\x1a\n" + b"\0" * (10 * 1024 * 1024 + 10)
        site = _Site({
            ("cdn.example", "/declared.png"): httpx.Response(
                200, headers={"content-type": "image/png", "content-length": str(len(huge))}, content=huge,
            ),
        })
        with self.assertRaises(FetchRefused) as caught:
            self._fetch(site.fetcher({"cdn.example": [PUBLIC_IP]}), "https://cdn.example/declared.png", image=True)
        self.assertEqual(caught.exception.code, "too_large")


PAGE = """<!doctype html><html><head>
<meta charset="utf-8"><title>VoltNest Wallbox – Official Store</title>
<meta name="description" content="Home EV charger">
<meta property="og:type" content="product">
<meta property="og:image" content="/media/hero.png">
<meta property="product:price:amount" content="399.00">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"Organization","name":"VoltNest"},
 {"@type":"Product","name":"VoltNest Wallbox","brand":{"@type":"Brand","name":"VoltNest"},
  "description":"Wall-mounted charger","image":["https://cdn.shop.example/a.png",{"@type":"ImageObject","url":"/media/b.png"}],
  "additionalProperty":[{"@type":"PropertyValue","name":"Power","value":"11 kW"}],
  "offers":{"@type":"Offer","price":"399.00","priceCurrency":"EUR"}}]}</script>
<style>.x{}</style></head><body>
<nav>Home | Shop | Cart</nav>
<h1>VoltNest Wallbox</h1><p>Charge three times faster than a wall socket.</p>
<img src="/media/logo.png" alt="logo"><img src="/icons/cart.png" width="24" height="24">
<img srcset="/media/c-400.png 400w, /media/c-1200.png 1200w" alt="side view">
<img data-src="/media/d.png" alt="in use"><img src="data:image/png;base64,AAAA">
<img src="/media/anim.gif"><footer>© VoltNest</footer>
<script>window.price = 399</script></body></html>"""


class TestPageParsing(unittest.TestCase):
    def test_json_ld_og_text_and_images(self):
        page = parse_product_page(PAGE, "https://shop.example/p/wallbox")
        self.assertTrue(page.has_product_signal)
        self.assertEqual(page.title, "VoltNest Wallbox – Official Store")
        self.assertEqual(len(page.json_ld_products), 1)
        product = page.json_ld_products[0]
        self.assertEqual(product["name"], "VoltNest Wallbox")
        self.assertEqual(product["brand"], "VoltNest")
        self.assertEqual(product["specs"], {"Power": "11 kW"})
        self.assertNotIn("offers", product)
        self.assertIn("Charge three times faster", page.main_text)
        self.assertNotIn("Home | Shop | Cart", page.main_text)
        self.assertNotIn("window.price", page.main_text)
        urls = [image.url for image in page.images]
        self.assertEqual(urls, [
            "https://shop.example/media/hero.png",
            "https://cdn.shop.example/a.png",
            "https://shop.example/media/b.png",
            "https://shop.example/media/c-1200.png",
            "https://shop.example/media/d.png",
        ])
        self.assertEqual([image.source for image in page.images], ["og", "json_ld", "json_ld", "img", "img"])

    def test_digest_never_carries_prices(self):
        page = parse_product_page(PAGE, "https://shop.example/p/wallbox")
        digest = page_digest_text(page, max_chars=10_000)
        self.assertIn("VoltNest Wallbox", digest)
        self.assertNotIn("399", digest)
        self.assertNotIn("EUR", digest)
        self.assertLessEqual(len(page_digest_text(page, max_chars=200)), 200)

    def test_malformed_markup_still_parses(self):
        page = parse_product_page("<html><body><p>half <b>open", "https://x.example/")
        self.assertIn("half", page.main_text)
        self.assertFalse(page.has_product_signal)


class TestExtractionNormalization(unittest.TestCase):
    def _answer(self, **overrides) -> dict:
        answer = {
            "is_product": True,
            "name": "  VoltNest   Wallbox ",
            "description": "Wall charger.",
            "model_variants": [{"label": "7kW", "specs": [{"name": "Power", "value": "7 kW"}]},
                               {"label": "7kW", "specs": []}],
            "specs": [{"name": "Weight", "value": "3 kg"}, {"name": "Price", "value": "399 EUR"},
                      {"name": "售价", "value": "¥2999"}],
            "selling_points": ["Fast", "Fast", "Safe", ""],
            "audiences": [{"who": "EV owners", "why": "home charging", "pain": "slow"}],
            "visual_must_match": [{"type": "color", "description": "matte black"}],
            "image_ranking": [{"image_id": "img_2", "role": "main"}, {"image_id": "img_9", "role": "main"}],
        }
        answer.update(overrides)
        return answer

    def test_schema_is_strict_mode_compatible_and_the_mock_satisfies_it(self):
        from app.providers import json_schema
        from app.providers.llm_types import ResolvedLlmChatRequest

        json_schema.check_schema(extraction.PRODUCT_INTAKE_SCHEMA)

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node.get("additionalProperties", True))
                    self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(extraction.PRODUCT_INTAKE_SCHEMA)
        request = ResolvedLlmChatRequest(
            semantic=extraction.SEMANTIC, provider="mock", model="mock-llm-v1", messages=(),
            images=(object(), object()), json_schema=extraction.PRODUCT_INTAKE_SCHEMA,
            schema_name="product_card", max_output_tokens=100, estimated_input_tokens=1,
            request_digest="d", redacted_request_digest="r",
        )
        answer = extraction._mock_product_intake(request)
        self.assertEqual(json_schema.validate(answer, extraction.PRODUCT_INTAKE_SCHEMA), [])
        self.assertEqual([item["image_id"] for item in answer["image_ranking"]], ["img_1", "img_2"])

    def test_normalization_bounds_dedupes_and_drops_commercial_specs(self):
        card = extraction.normalize_extraction(self._answer(), candidate_ids=["img_1", "img_2"])
        self.assertEqual(card["name"], "VoltNest Wallbox")
        self.assertEqual(card["specs"], {"Weight": "3 kg"})
        self.assertEqual(card["dropped_commercial_specs"], ["Price", "售价"])
        self.assertEqual(card["selling_points"], ["Fast", "Safe"])
        self.assertEqual([v["label"] for v in card["model_variants"]], ["7kW"])
        self.assertEqual(card["model_variants"][0]["specs"], {"Power": "7 kW"})
        self.assertEqual(card["image_ranking"], [{"image_id": "img_2", "role": "main"}])
        must_match = card["must_match"][0]
        self.assertEqual((must_match["proposed_by"], must_match["confirmed"]), ("ai", False))
        self.assertTrue(must_match["id"].startswith("mm_"))

    def test_colour_editions_become_variants_with_their_appearance_and_images(self):
        """Issue #192: per-variant appearance and images, de-duplicated and bounded."""
        shared_colour = {"type": "color", "description": "Colours: Classic Black, Avalanche White, Camo"}
        answer = self._answer(
            visual_must_match=[shared_colour, {"type": "logo", "description": "paw logo centred"}],
            model_variants=[
                {"label": " Classic  Black ", "specs": [], "image_ids": ["img_2", "img_2", "img_9", " img_4 "],
                 "appearance": [
                     {"type": "color", "description": "matte black housing, blue paw logo"},
                     {"type": "color", "description": "matte black housing, blue paw logo"},
                     {"type": "logo", "description": "paw logo centred"},  # same as a shared item
                     {"type": "material", "description": "   "},
                 ]},
                {"label": "Avalanche White", "specs": [{"name": "Finish", "value": "gloss"}],
                 "image_ids": ["img_3", "img_4"],
                 "appearance": [{"type": "color", "description": "gloss white housing"}]},
                {"label": "Camo", "specs": [], "image_ids": [], "appearance": [
                    {"type": "visual", "description": f"camo pattern {n}"} for n in range(10)
                ]},
            ],
        )
        card = extraction.normalize_extraction(answer, candidate_ids=["img_1", "img_2", "img_3", "img_4"])
        black, white, camo = card["model_variants"]
        self.assertEqual(black["label"], "Classic Black")
        self.assertEqual([item["description"] for item in black["appearance"]], ["matte black housing, blue paw logo"])
        self.assertEqual((black["appearance"][0]["proposed_by"], black["appearance"][0]["confirmed"]), ("ai", False))
        # img_9 is unknown; img_4 is claimed by two variants, so it is general.
        self.assertEqual(black["image_ids"], ["img_2"])
        self.assertEqual(white["image_ids"], ["img_3"])
        self.assertEqual(white["specs"], {"Finish": "gloss"})
        self.assertEqual(len(camo["appearance"]), extraction.MAX_VARIANT_APPEARANCE)
        self.assertEqual(camo["image_ids"], [])
        # The shared list keeps its colour line; choosing a variant replaces it.
        self.assertEqual([item["type"] for item in card["must_match"]], ["color", "logo"])
        shared_colour_id = card["must_match"][0]["id"]

        composed, replaced = extraction.variant_must_match(card["must_match"], white)
        self.assertEqual(replaced, [shared_colour_id])
        self.assertEqual([item["description"] for item in composed], ["paw logo centred", "gloss white housing"])
        # A variant without its own colour keeps the shared colour line.
        composed, replaced = extraction.variant_must_match(card["must_match"], camo)
        self.assertEqual(replaced, [])
        self.assertEqual(composed[:2], card["must_match"])
        self.assertEqual(len(composed), 2 + extraction.MAX_VARIANT_APPEARANCE)
        self.assertEqual(extraction.variant_must_match(card["must_match"], None), (card["must_match"], []))

        ranked = ["img_1", "img_2", "img_3", "img_4"]
        variants = card["model_variants"]
        self.assertEqual(extraction.variant_image_ids(variants, "Avalanche White", ranked), ["img_1", "img_3", "img_4"])
        self.assertEqual(extraction.variant_image_ids(variants, "Camo", ranked), ["img_1", "img_4"])
        self.assertEqual(extraction.variant_image_ids(variants, None, ranked), ranked)

    def test_variants_are_capped_and_single_model_answers_are_unchanged(self):
        from app.library.contracts import PRODUCT_VARIANTS_MAX

        many = [{"label": f"Colour {n}", "specs": [], "image_ids": [], "appearance": []} for n in range(30)]
        card = extraction.normalize_extraction(self._answer(model_variants=many), candidate_ids=[])
        self.assertEqual(len(card["model_variants"]), PRODUCT_VARIANTS_MAX)
        card = extraction.normalize_extraction(self._answer(model_variants=[]), candidate_ids=["img_2"])
        self.assertEqual(card["model_variants"], [])
        self.assertEqual(card["must_match"][0]["description"], "matte black")

    def test_mock_offers_colour_editions_when_the_user_mentions_colours(self):
        from app.providers import json_schema
        from app.providers.llm_types import LlmMessage, ResolvedLlmChatRequest

        def request(text: str, images: int) -> ResolvedLlmChatRequest:
            return ResolvedLlmChatRequest(
                semantic=extraction.SEMANTIC, provider="mock", model="mock-llm-v1",
                messages=(LlmMessage("system", extraction.SYSTEM_PROMPT), LlmMessage("user", text)),
                images=tuple(object() for _ in range(images)), json_schema=extraction.PRODUCT_INTAKE_SCHEMA,
                schema_name="product_card", max_output_tokens=100, estimated_input_tokens=1,
                request_digest="d", redacted_request_digest="r",
            )

        colour = extraction._mock_product_intake(request("VoltNest 充电桩，有三种配色", 5))
        self.assertEqual(json_schema.validate(colour, extraction.PRODUCT_INTAKE_SCHEMA), [])
        self.assertEqual([v["label"] for v in colour["model_variants"]], ["曜石黑", "冰川白", "丛林迷彩"])
        self.assertEqual([v["image_ids"] for v in colour["model_variants"]], [["img_2", "img_5"], ["img_3"], ["img_4"]])
        card = extraction.normalize_extraction(colour, candidate_ids=[f"img_{n}" for n in range(1, 6)])
        white = card["model_variants"][1]
        composed, replaced = extraction.variant_must_match(card["must_match"], white)
        self.assertEqual(len(replaced), 1)
        self.assertNotIn("可选配色", " ".join(item["description"] for item in composed))
        self.assertIn("亮面白色", composed[-1]["description"])

        # The system prompt mentions colours too; only the user's text selects the example.
        power = extraction._mock_product_intake(request("VoltNest 充电桩，7kW 和 11kW", 1))
        self.assertEqual([v["label"] for v in power["model_variants"]], ["7kW 单相", "11kW 三相"])
        self.assertTrue(all(v["appearance"] == [] and v["image_ids"] == [] for v in power["model_variants"]))

    def test_prompt_lists_images_that_are_not_attached_by_url(self):
        prompt = extraction.build_user_prompt(
            user_text=None, page_url="https://shop.example/p", page_digest="page",
            image_labels=[{"image_id": "img_1", "source": "product_page", "url": "https://shop.example/a.png"}],
            other_image_labels=[{"image_id": "img_7", "source": "product_page",
                                 "url": "https://shop.example/camo.png", "alt": "Camo edition"}],
        )
        self.assertIn("img_1: from product_page, url: https://shop.example/a.png", prompt)
        self.assertIn("img_7: from product_page, url: https://shop.example/camo.png, alt text: Camo edition", prompt)
        self.assertIn("not attached", prompt)

    def test_unusable_answers_are_refused(self):
        for overrides, problem in (({"name": "   "}, "name is empty"),
                                   ({"selling_points": []}, "no selling point"),
                                   ({"audiences": []}, "no audience")):
            with self.subTest(problem=problem):
                with self.assertRaises(extraction.ExtractionInvalid) as caught:
                    extraction.normalize_extraction(self._answer(**overrides), candidate_ids=[])
                self.assertIn(problem, caught.exception.problems)

    def test_prompt_and_reservation_budget_cover_the_largest_request(self):
        from app.providers.llm_chat import estimate_request_input_tokens
        from app.providers.llm_chat import LlmChatRequest, LlmImage
        from app.providers.llm_registry import model_spec

        prompt = extraction.build_user_prompt(
            user_text="字" * 50_000, page_url="https://shop.example/", page_digest="x" * 50_000,
            image_labels=[{"image_id": f"img_{i}", "source": "upload"} for i in range(1, 7)],
        )
        self.assertLessEqual(len(prompt.encode("utf-8")), extraction.MAX_USER_PROMPT_BYTES)
        request = LlmChatRequest.build(
            semantic=extraction.SEMANTIC, system=extraction.SYSTEM_PROMPT, user=prompt,
            json_schema=extraction.PRODUCT_INTAKE_SCHEMA, schema_name=extraction.SCHEMA_NAME,
            max_output_tokens=extraction.MAX_OUTPUT_TOKENS,
            images=tuple(LlmImage.from_bytes(_png()) for _ in range(extraction.MAX_LLM_IMAGES)),
        )
        spec = model_spec("openai", None)
        self.assertLessEqual(
            estimate_request_input_tokens(request, execution_mode="byok"),
            extraction.input_token_budget(spec),
        )


class TestProductContentRules(unittest.TestCase):
    BASE = {"name": "Wallbox", "description": "Charger", "audience": "EV owners", "selling_points": ["fast"]}

    def _codes(self, **content) -> set[str]:
        report = validate_content(ENTRY_PRODUCT, {**self.BASE, **content})
        return {finding.code for finding in report.blocking}

    def test_new_fields_validate(self):
        self.assertEqual(self._codes(
            specs={"Power": "11 kW"},
            model_variants=[{"label": "11kW", "specs": {"Input": "400 V"}}],
            audiences=[{"who": "EV owners", "why": "home", "pain": "slow"}],
            source_url="https://shop.example/p/1",
            must_match=[extraction.must_match_item("color", "black", proposed_by="ai", confirmed=True)],
        ), set())

    def test_commercial_truth_and_bad_shapes_are_blocked(self):
        self.assertIn("product_spec_commercial_truth", self._codes(specs={"Price": "399"}))
        self.assertIn("product_spec_commercial_truth", self._codes(
            model_variants=[{"label": "a", "specs": {"现价": "1"}}]))
        self.assertIn("product_specs_shape", self._codes(specs=["Power"]))
        self.assertIn("product_spec_value", self._codes(specs={"Power": 11}))
        self.assertIn("product_variant_duplicate", self._codes(
            model_variants=[{"label": "a", "specs": {}}, {"label": "a", "specs": {}}]))
        self.assertIn("product_audience_who", self._codes(audiences=[{"why": "x"}]))
        self.assertIn("product_source_url", self._codes(source_url="javascript:alert(1)"))
        self.assertIn("forbidden_field", self._codes(landing_page_url="https://shop.example/"))
        self.assertIn("must_match_proposed_by", self._codes(
            must_match=[{**extraction.must_match_item("color", "x", proposed_by="ai", confirmed=True),
                         "proposed_by": "robot"}]))
        self.assertIn("must_match_unconfirmed", self._codes(
            must_match=[extraction.must_match_item("color", "x", proposed_by="ai", confirmed=False)]))


if __name__ == "__main__":
    unittest.main()
