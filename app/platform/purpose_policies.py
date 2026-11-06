"""Purpose-based MIME allowlist, size limit, and default TTL (spec §7.5).

``Content-Type`` in storage metadata is whatever the client declared -- it is
not the actual file type. The Worker verifies the real type via a magic-byte
sniff (:func:`app.platform.upload_intents.sniff_mime`) during finalize and
checks the result against ``allowed_mimes`` here. A verified MIME outside the
purpose's allowlist is rejected with ``mime_type_not_allowed`` (spec §7.9).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PurposePolicy:
    allowed_mimes: frozenset[str]
    max_size_bytes: int
    default_ttl_seconds: int


PURPOSE_POLICIES: dict[str, PurposePolicy] = {
    "generation_output": PurposePolicy(
        allowed_mimes=frozenset({"image/png", "image/jpeg", "image/webp"}),
        max_size_bytes=50 * 1024 * 1024,
        default_ttl_seconds=300,
    ),
    "reference_asset": PurposePolicy(
        allowed_mimes=frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
        max_size_bytes=100 * 1024 * 1024,
        default_ttl_seconds=300,
    ),
    # Product intake (Issue #161): screenshots/photos the user pastes, and
    # the product-page images the Worker downloads and commits server-side
    # under the same purpose. Same image types as the Library reference gate
    # accepts (no GIF: the gate's decoder refuses it); 10 MB, the front-flow
    # contract's per-image bound.
    "product_intake_image": PurposePolicy(
        allowed_mimes=frozenset({"image/png", "image/jpeg", "image/webp"}),
        max_size_bytes=10 * 1024 * 1024,
        default_ttl_seconds=300,
    ),
    # External creatives (Issue #168): finished ad images made elsewhere
    # (Lovart, a designer, real UGC) that join tests. Same image types as
    # the reference gate decodes; 20 MB, the gate's own byte ceiling
    # (``LIBRARY_MAX_REFERENCE_BYTES``), so nothing the policy admits is
    # refused later for size alone.
    "external_creative": PurposePolicy(
        allowed_mimes=frozenset({"image/png", "image/jpeg", "image/webp"}),
        max_size_bytes=20 * 1024 * 1024,
        default_ttl_seconds=300,
    ),
    "library_manual_fact_import": PurposePolicy(
        allowed_mimes=frozenset({"application/json", "text/csv"}),
        max_size_bytes=10 * 1024 * 1024,
        default_ttl_seconds=300,
    ),
    # Ad-level results sheet (Issue #166): a CSV the seller exports from Ads
    # Manager or fills from the template. UTF-8 only at this layer (the
    # browser re-encodes a GB18030 sheet before uploading).
    "results_import": PurposePolicy(
        allowed_mimes=frozenset({"text/csv"}),
        max_size_bytes=10 * 1024 * 1024,
        default_ttl_seconds=300,
    ),
    "export_package": PurposePolicy(
        allowed_mimes=frozenset({"application/zip"}),
        max_size_bytes=500 * 1024 * 1024,
        default_ttl_seconds=600,
    ),
}
