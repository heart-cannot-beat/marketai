"""List prices for the real image providers the Generation Run worker dispatches.

The preview quotes ``Maximum cost`` from these numbers and Atomic Submission
reserves spend against them (``app.entitlements.quote_engine`` is keyed by
provider + model only, so the format-dependent unit price is resolved here
first and handed over as the model's price for that one Run). The settlement
recorded after a successful call is this estimate -- the spec's "estimated
settlement, adjusted later when a reported cost arrives".

``PRICE_RULE_VERSION`` is bumped whenever any table changes so every quote
records which revision priced it. ``image-prices-2026-10`` added reference-
image calls (OpenAI image edits, Gemini and Seedream with input images) and
the Gemini / Seedream tables.

* **OpenAI GPT-Image** is billed per output image by quality and size
  (OpenAI pricing page, checked 2026-09). A call that sends reference images
  (``/v1/images/edits``) additionally pays for the input images as image
  input tokens; :data:`OPENAI_INPUT_IMAGE_PRICES` is a conservative upper
  bound per input image at ``input_fidelity=high``.
* **Gemini image** (``generateContent``) is billed per output image by tier
  plus input image tokens.
* **Seedream** (Volcengine Ark) is billed per output image; input reference
  images are not billed separately.

UNVERIFIED: the input-image surcharges, every Gemini price and every
Seedream price are estimates written down without network access on
2026-10-12 (Seedream is billed in CNY and converted here to USD at a
rounded-up rate). They are deliberately conservative -- a reservation is a
ceiling -- and must be checked against the providers' price pages before
real spend relies on them.
"""
from __future__ import annotations

from decimal import Decimal

PRICE_RULE_VERSION = "image-prices-2026-10"

OPENAI_PROVIDER = "openai"
GEMINI_PROVIDER = "gemini"
SEEDREAM_PROVIDER = "seedream"

#: Draft aspect ratio -> OpenAI output size. GPT-Image renders exactly three
#: sizes; portrait and landscape ratios snap to the nearest one.
OPENAI_SIZE_FOR_ASPECT_RATIO: dict[str, str] = {
    "1:1": "1024x1024",
    "4:5": "1024x1536",
    "9:16": "1024x1536",
    "2:3": "1024x1536",
    "3:4": "1024x1536",
    "16:9": "1536x1024",
    "3:2": "1536x1024",
    "4:3": "1536x1024",
}
OPENAI_DEFAULT_SIZE = "1024x1024"
OPENAI_QUALITIES = ("low", "medium", "high")
OPENAI_DEFAULT_QUALITY = "medium"

#: USD per generated image: model -> quality -> size.
OPENAI_IMAGE_PRICES: dict[str, dict[str, dict[str, Decimal]]] = {
    "gpt-image-2": {
        "low": {"1024x1024": Decimal("0.006"), "1024x1536": Decimal("0.005"), "1536x1024": Decimal("0.005")},
        "medium": {"1024x1024": Decimal("0.053"), "1024x1536": Decimal("0.041"), "1536x1024": Decimal("0.041")},
        "high": {"1024x1024": Decimal("0.211"), "1024x1536": Decimal("0.165"), "1536x1024": Decimal("0.165")},
    },
    "gpt-image-1.5": {
        "low": {"1024x1024": Decimal("0.009"), "1024x1536": Decimal("0.013"), "1536x1024": Decimal("0.013")},
        "medium": {"1024x1024": Decimal("0.034"), "1024x1536": Decimal("0.050"), "1536x1024": Decimal("0.050")},
        "high": {"1024x1024": Decimal("0.133"), "1024x1536": Decimal("0.200"), "1536x1024": Decimal("0.200")},
    },
    "gpt-image-1": {
        "low": {"1024x1024": Decimal("0.011"), "1024x1536": Decimal("0.016"), "1536x1024": Decimal("0.016")},
        "medium": {"1024x1024": Decimal("0.042"), "1024x1536": Decimal("0.063"), "1536x1024": Decimal("0.063")},
        "high": {"1024x1024": Decimal("0.167"), "1024x1536": Decimal("0.250"), "1536x1024": Decimal("0.250")},
    },
    "gpt-image-1-mini": {
        "low": {"1024x1024": Decimal("0.005"), "1024x1536": Decimal("0.006"), "1536x1024": Decimal("0.006")},
        "medium": {"1024x1024": Decimal("0.011"), "1024x1536": Decimal("0.015"), "1536x1024": Decimal("0.015")},
        "high": {"1024x1024": Decimal("0.036"), "1024x1536": Decimal("0.052"), "1536x1024": Decimal("0.052")},
    },
}

#: USD per *input* reference image on ``/v1/images/edits`` (UNVERIFIED upper
#: bound: ~6.5k image input tokens at high input fidelity x the model's image
#: input token price, plus the instruction's text tokens).
OPENAI_INPUT_IMAGE_PRICES: dict[str, Decimal] = {
    "gpt-image-2": Decimal("0.060"),
    "gpt-image-1.5": Decimal("0.060"),
    "gpt-image-1": Decimal("0.070"),
    "gpt-image-1-mini": Decimal("0.020"),
}

#: Gemini ``imageConfig.imageSize`` tiers.
GEMINI_IMAGE_SIZES = ("1K", "2K", "4K")
GEMINI_DEFAULT_IMAGE_SIZE = "1K"

#: USD per generated image: model -> image size tier (UNVERIFIED).
GEMINI_IMAGE_PRICES: dict[str, dict[str, Decimal]] = {
    "gemini-3.1-flash-image": {"1K": Decimal("0.070"), "2K": Decimal("0.105"), "4K": Decimal("0.155")},
}

#: USD per input image (UNVERIFIED: ~560-1120 input tokens per image).
GEMINI_INPUT_IMAGE_PRICES: dict[str, Decimal] = {
    "gemini-3.1-flash-image": Decimal("0.002"),
}

#: USD per generated image (UNVERIFIED: Ark lists Seedream 4.5 at about
#: CNY 0.25 per image; converted with headroom).
SEEDREAM_IMAGE_PRICES: dict[str, Decimal] = {
    "doubao-seedream-4-5-251128": Decimal("0.040"),
}


class UnpricedCallError(LookupError):
    """No list price is known for the requested provider/model/format."""


def openai_size(format_config: dict | None) -> str:
    """The OpenAI output size a Run's format resolves to."""
    config = format_config or {}
    explicit = str(config.get("image_size") or config.get("resolution") or "").strip()
    if explicit in {"1024x1024", "1024x1536", "1536x1024"}:
        return explicit
    return OPENAI_SIZE_FOR_ASPECT_RATIO.get(str(config.get("aspect_ratio") or ""), OPENAI_DEFAULT_SIZE)


def openai_quality(format_config: dict | None) -> str:
    """The GPT-Image ``quality`` a Run's format asks for (``medium`` unless set)."""
    quality = str((format_config or {}).get("quality") or "").strip().lower()
    return quality if quality in OPENAI_QUALITIES else OPENAI_DEFAULT_QUALITY


def gemini_image_size(format_config: dict | None) -> str:
    """The Gemini ``imageSize`` tier a Run's format asks for (``1K`` unless set)."""
    config = format_config or {}
    for key in ("image_size", "resolution"):
        value = str(config.get(key) or "").strip().upper()
        if value in GEMINI_IMAGE_SIZES:
            return value
    return GEMINI_DEFAULT_IMAGE_SIZE


def unit_price(
    provider: str,
    model: str,
    format_config: dict | None,
    *,
    reference_images: int = 0,
) -> Decimal:
    """USD list price of one call for ``provider``/``model`` at this format.

    ``reference_images`` is how many product images the call sends (the
    recipe's frozen ``reference_policy.count``); ``0`` prices a text-only call.
    """
    references = max(int(reference_images or 0), 0)
    if provider == OPENAI_PROVIDER:
        by_quality = OPENAI_IMAGE_PRICES.get(model)
        if by_quality is None:
            raise UnpricedCallError(f"no list price for OpenAI model {model!r}")
        price = by_quality[openai_quality(format_config)][openai_size(format_config)]
        if references:
            price += OPENAI_INPUT_IMAGE_PRICES[model] * references
        return price
    if provider == GEMINI_PROVIDER:
        tiers = GEMINI_IMAGE_PRICES.get(model)
        if tiers is None:
            raise UnpricedCallError(f"no list price for Gemini model {model!r}")
        price = tiers[gemini_image_size(format_config)]
        if references:
            price += GEMINI_INPUT_IMAGE_PRICES[model] * references
        return price
    if provider == SEEDREAM_PROVIDER:
        price = SEEDREAM_IMAGE_PRICES.get(model)
        if price is None:
            raise UnpricedCallError(f"no list price for Seedream model {model!r}")
        return price
    raise UnpricedCallError(f"no list price for provider {provider!r}")


def price_registry_for(
    provider: str,
    model: str,
    format_config: dict | None,
    *,
    reference_images: int = 0,
) -> dict[str, dict[str, Decimal]]:
    """A ``CostQuoteEngine`` registry that prices exactly this Run's calls."""
    return {provider: {model: unit_price(provider, model, format_config, reference_images=reference_images)}}
