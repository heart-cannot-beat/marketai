"""SSRF-safe fetching of a pasted product page and its images (Issue #161).

Front-flow contract §6 bounds what product intake may fetch: http(s) only;
after DNS resolution refuse private, loopback, link-local and cloud-metadata
addresses -- on every redirect hop; at most 3 redirects; 10 s; a body of at
most 5 MB; at most 12 images of at most 10 MB each; a fixed User-Agent.

How each bound is enforced here:

* **Scheme, port, credentials.** Only ``http``/``https`` on ports 80/443,
  no ``user:pass@`` userinfo, no ``localhost``-style or metadata host names.
* **Resolve, check, pin.** The host is resolved once per hop; if *any*
  resolved address is not globally routable the hop is refused (a mixed
  answer is how DNS rebinding hides a private address), and the request is
  sent to the checked address itself -- the URL carries the IP, ``Host`` and
  TLS SNI carry the name, and certificate verification uses the name. So the
  address that was checked is the address that is connected to: there is no
  second resolution between check and connect for an attacker's DNS to
  answer differently (the TOCTOU of "check then let the client resolve").
  IPv4-mapped/NAT64/6to4/Teredo IPv6 forms are unwrapped to the IPv4 they
  embed before the check. Environment proxies are ignored (``trust_env``
  off) so a proxy can never be the one resolving.
* **Redirects** are followed by hand (``follow_redirects`` off), each new
  location parsed and checked exactly like the first URL, at most 3.
* **Size and time.** Bodies are streamed and cut at the limit (a page is
  truncated and marked so; an image over its limit is dropped); a page fetch
  including all its hops is bounded by one 10 s deadline, each image by its
  own.
* **Content type.** A page must be HTML (or an image, when the pasted link
  points straight at one); an image must be PNG/JPEG/WebP by header *and*
  by its magic bytes.

Refusals raise :class:`FetchRefused` with a stable ``code`` (what the UI
keys its copy on) and, for an address refusal, only the *category* of the
blocked address (``loopback``, ``private``, ``metadata`` ...) -- never the
resolved IP, which is what the audit records.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

USER_AGENT = "MarketAI-ProductIntake/1.0 (+product card import)"
PAGE_TIMEOUT_SECONDS = 10.0
IMAGE_TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 3
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 12
MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}
ALLOWED_PORTS = frozenset({80, 443})

HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
IMAGE_CONTENT_TYPES = ("image/png", "image/jpeg", "image/webp")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Host names that never leave the machine or name a metadata service,
#: refused before any resolution.
_LOOPBACK_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback")
_METADATA_NAMES = ("metadata", "metadata.google.internal", "metadata.goog", "instance-data")

#: Cloud instance-metadata endpoints (AWS/GCP/Azure/OpenStack link-local,
#: AWS ECS task metadata, AWS IPv6 IMDS, Alibaba Cloud, Oracle Cloud).
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(address)
    for address in (
        "169.254.169.254", "169.254.170.2", "169.254.169.123",
        "fd00:ec2::254", "100.100.100.200", "192.0.0.192",
    )
)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")

#: ``(host, port) -> addresses``; injectable so tests answer DNS themselves.
Resolver = Callable[[str, int], Awaitable[list[str]]]


class FetchRefused(Exception):
    """A fetch that was refused or failed, with a stable, UI-facing ``code``.

    ``category`` is set for ``blocked_address`` (the kind of address, never
    the address itself). ``retryable`` says whether the same fetch may
    succeed later (timeouts, 5xx, 429, connection failures).
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        category: str | None = None,
        retryable: bool = False,
        status_code: int | None = None,
        hops: list[dict] | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.hops = list(hops or [])
        super().__init__(f"{code}: {detail}")

    def as_evidence(self) -> dict:
        evidence: dict = {"code": self.code, "detail": self.detail, "retryable": self.retryable}
        if self.category:
            evidence["blocked_category"] = self.category
        if self.status_code is not None:
            evidence["status_code"] = self.status_code
        if self.hops:
            evidence["hops"] = list(self.hops)
        return evidence


@dataclass(frozen=True)
class Target:
    """One parsed, policy-checked URL."""

    url: str
    scheme: str
    host: str  # IDNA/ASCII, lowercase, no brackets
    port: int
    path_query: str
    is_ip_literal: bool

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == DEFAULT_PORTS[self.scheme] else f"{host}:{self.port}"


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes = field(repr=False)
    truncated: bool
    hops: list[dict]
    elapsed_ms: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def kind(self) -> str:
        if self.content_type in IMAGE_CONTENT_TYPES:
            return "image"
        return "html"


# ---------------------------------------------------------------------------
# Address policy
# ---------------------------------------------------------------------------


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in _NAT64 or address in _NAT64_LOCAL:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    return None


def blocked_category(address: str) -> str | None:
    """``None`` for a globally routable unicast address, else why it is refused."""
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "invalid"
    if isinstance(parsed, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(parsed)
        if embedded is not None:
            category = blocked_category(str(embedded))
            if category is not None:
                return category
    if parsed in _METADATA_ADDRESSES:
        return "metadata"
    if parsed.is_loopback:
        return "loopback"
    if parsed.is_link_local:
        return "link_local"
    if isinstance(parsed, ipaddress.IPv4Address) and parsed in _CGNAT:
        return "cgnat"
    if parsed.is_multicast:
        return "multicast"
    if parsed.is_unspecified:
        return "unspecified"
    if parsed.is_private:
        # RFC 1918, IPv6 unique-local (fc00::/7), documentation and other
        # special-purpose ranges Python classifies as private.
        return "private"
    if parsed.is_reserved:
        return "reserved"
    if not parsed.is_global:
        return "non_global"
    return None


def parse_target(url: str) -> Target:
    """Parse ``url`` and apply the scheme/port/credential/host-name policy."""
    raw = (url or "").strip()
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise FetchRefused("invalid_url", "the link is empty or longer than 2048 characters")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise FetchRefused("invalid_url", f"the link cannot be parsed: {exc}") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise FetchRefused("scheme_not_allowed", "only http and https links can be read")
    if parts.username is not None or parts.password is not None:
        raise FetchRefused("credentials_in_url", "links with a user name or password are not read")
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise FetchRefused("invalid_url", "the link has no host")
    is_ip_literal = True
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        is_ip_literal = False
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise FetchRefused("invalid_url", "the link's host name is not valid") from exc
    port = port or DEFAULT_PORTS[scheme]
    if port not in ALLOWED_PORTS:
        raise FetchRefused("port_not_allowed", "only the standard ports 80 and 443 are read")
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        raise FetchRefused("blocked_address", "the link points at this machine", category="loopback")
    if host in _METADATA_NAMES:
        raise FetchRefused("blocked_address", "the link points at a metadata service", category="metadata")
    path_query = parts.path or "/"
    if parts.query:
        path_query = f"{path_query}?{parts.query}"
    canonical = urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))
    return Target(
        url=canonical, scheme=scheme, host=host, port=port,
        path_query=path_query, is_ip_literal=is_ip_literal,
    )


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class SafeFetcher:
    """Fetches one URL under the policy above; see the module docstring."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        page_timeout: float = PAGE_TIMEOUT_SECONDS,
        image_timeout: float = IMAGE_TIMEOUT_SECONDS,
    ) -> None:
        self._resolver = resolver or system_resolver
        self._transport = transport
        self._page_timeout = page_timeout
        self._image_timeout = image_timeout

    async def fetch_page(self, url: str) -> FetchResult:
        """The product page (or a direct image link), redirects included, in 10 s."""
        return await self._bounded(
            self._fetch(url, accept="html", max_bytes=MAX_PAGE_BYTES, truncate=True),
            self._page_timeout,
        )

    async def fetch_image(self, url: str) -> FetchResult:
        """One image, at most 10 MB, PNG/JPEG/WebP, in 10 s."""
        return await self._bounded(
            self._fetch(url, accept="image", max_bytes=MAX_IMAGE_BYTES, truncate=False),
            self._image_timeout,
        )

    @staticmethod
    async def _bounded(coro, timeout: float) -> FetchResult:
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise FetchRefused(
                "timeout", f"the site did not answer within {timeout:g} s", retryable=True,
            ) from exc

    async def _resolve(self, target: Target) -> str:
        if target.is_ip_literal:
            addresses = [target.host]
        else:
            try:
                addresses = await self._resolver(target.host, target.port)
            except (OSError, UnicodeError) as exc:
                raise FetchRefused(
                    "dns_failed", f"the host name could not be resolved ({type(exc).__name__})",
                    retryable=True,
                ) from exc
        if not addresses:
            raise FetchRefused("dns_failed", "the host name has no address", retryable=True)
        for address in addresses:
            category = blocked_category(address)
            if category is not None:
                raise FetchRefused(
                    "blocked_address",
                    "the link resolves to an address that is not on the public internet",
                    category=category,
                )
        return addresses[0].split("%", 1)[0]

    async def _fetch(self, url: str, *, accept: str, max_bytes: int, truncate: bool) -> FetchResult:
        started = time.monotonic()
        requested = url
        hops: list[dict] = []
        current = parse_target(url)
        for hop_no in range(MAX_REDIRECTS + 1):
            try:
                address = await self._resolve(current)
            except FetchRefused as refused:
                refused.hops = hops + [{"url": current.url, "refused": refused.code}]
                raise
            outcome = await self._send(current, address, accept=accept, max_bytes=max_bytes, truncate=truncate)
            status, location, content_type, body, truncated = outcome
            hops.append({"url": current.url, "status": status})
            if status in _REDIRECT_STATUSES and location:
                if hop_no == MAX_REDIRECTS:
                    raise FetchRefused(
                        "too_many_redirects", f"more than {MAX_REDIRECTS} redirects", hops=hops,
                    )
                try:
                    current = parse_target(urljoin(current.url, location))
                except FetchRefused as refused:
                    refused.hops = hops
                    raise
                continue
            self._check_status(status, hops)
            self._check_content_type(content_type, body, accept=accept, hops=hops)
            return FetchResult(
                requested_url=requested,
                final_url=current.url,
                status_code=status,
                content_type=content_type,
                body=body,
                truncated=truncated,
                hops=hops,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        raise FetchRefused("too_many_redirects", f"more than {MAX_REDIRECTS} redirects", hops=hops)

    async def _send(
        self, target: Target, address: str, *, accept: str, max_bytes: int, truncate: bool,
    ) -> tuple[int, str | None, str, bytes, bool]:
        netloc = f"[{address}]" if ":" in address else address
        if target.port != DEFAULT_PORTS[target.scheme]:
            netloc = f"{netloc}:{target.port}"
        pinned_url = urlunsplit((target.scheme, netloc, "", "", "")) + target.path_query
        headers = {
            "Host": target.host_header,
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml;q=0.9,image/*;q=0.5"
                if accept == "html" else "image/png,image/jpeg,image/webp;q=0.9"
            ),
        }
        extensions = {}
        if target.scheme == "https" and not target.is_ip_literal:
            # Certificate verification and SNI use the name; the socket goes
            # to the checked address.
            extensions["sni_hostname"] = target.host
        try:
            async with httpx.AsyncClient(
                transport=self._transport, trust_env=False, follow_redirects=False, timeout=None,
            ) as client:
                request = client.build_request("GET", pinned_url, headers=headers, extensions=extensions)
                response = await client.send(request, stream=True)
                try:
                    status = response.status_code
                    location = response.headers.get("location")
                    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                    if status in _REDIRECT_STATUSES or not 200 <= status < 300:
                        return status, location, content_type, b"", False
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes and not truncate:
                        raise FetchRefused(
                            "too_large", f"the file is larger than {max_bytes // (1024 * 1024)} MB",
                        )
                    chunks: list[bytes] = []
                    size = 0
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        if size + len(chunk) > max_bytes:
                            if not truncate:
                                raise FetchRefused(
                                    "too_large",
                                    f"the file is larger than {max_bytes // (1024 * 1024)} MB",
                                )
                            chunks.append(chunk[: max_bytes - size])
                            truncated = True
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                    return status, location, content_type, b"".join(chunks), truncated
                finally:
                    await response.aclose()
        except FetchRefused:
            raise
        except httpx.TimeoutException as exc:
            raise FetchRefused("timeout", "the site did not answer in time", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise FetchRefused(
                "connection_failed", f"the site could not be reached ({type(exc).__name__})",
                retryable=True,
            ) from exc

    @staticmethod
    def _check_status(status: int, hops: list[dict]) -> None:
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise FetchRefused(
                "access_denied", f"the site refused automated reading (HTTP {status})",
                status_code=status, hops=hops,
            )
        if status == 429:
            raise FetchRefused(
                "access_denied", "the site is rate limiting automated reading (HTTP 429)",
                status_code=status, retryable=True, hops=hops,
            )
        if status in (404, 410):
            raise FetchRefused(
                "not_found", f"the page does not exist (HTTP {status})", status_code=status, hops=hops,
            )
        if status >= 500:
            raise FetchRefused(
                "http_error", f"the site failed to answer (HTTP {status})",
                status_code=status, retryable=True, hops=hops,
            )
        raise FetchRefused(
            "http_error", f"the site answered HTTP {status}", status_code=status, hops=hops,
        )

    @staticmethod
    def _check_content_type(content_type: str, body: bytes, *, accept: str, hops: list[dict]) -> None:
        if content_type in IMAGE_CONTENT_TYPES:
            sniffed = sniff_image(body)
            if sniffed is None or sniffed != content_type:
                raise FetchRefused(
                    "unsupported_content_type",
                    "the file does not contain the image type it declares",
                    hops=hops,
                )
            return
        if accept == "html" and content_type in HTML_CONTENT_TYPES:
            return
        raise FetchRefused(
            "unsupported_content_type",
            f"the link returned {content_type or 'an unknown type'}, not a product page or image",
            hops=hops,
        )


def sniff_image(content: bytes) -> str | None:
    """PNG/JPEG/WebP by magic bytes (the only types intake keeps)."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None
