"""Chat models ``llm_chat`` can dispatch to, and what each token costs.

One registry entry per model: who serves it, whether it reads images, its
per-million-token list prices, and the ceilings the governed path needs to
quote a *maximum* cost before the call is made (ADR 0005, contract §11):

* ``max_output_tokens`` -- the largest ``max_output_tokens`` a caller may
  ask for (a platform cap well under the model's own limit, so one call can
  never reserve more than a few cents);
* ``image_input_tokens_ceiling`` -- an upper bound on what one input image
  can cost in input tokens at ``detail: auto``, from OpenAI's vision sizing
  rules (patch-based models top out at 1536 patches times the model's
  multiplier; ``gpt-4o-mini`` bills images at a fixed high rate).

Prices are USD per 1M tokens, Standard tier, checked 2026-10 against the
published OpenAI price list. ``LLM_PRICE_RULE_VERSION`` is bumped whenever
any number here changes; every quote, usage report and settlement records
which revision priced it. Reasoning tokens are billed (and capped by
``max_completion_tokens``) as output tokens, so they need no separate price.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Mapping

LLM_PRICE_RULE_VERSION = "openai-chat-2026-10"

OPENAI_PROVIDER = "openai"
MOCK_PROVIDER = "mock"

#: An answer that fails JSON-schema validation is re-asked once
#: (``app.providers.openai_chat``), so one ``llm_chat`` call can bill up to
#: ``1 + LLM_SCHEMA_RETRIES`` provider requests and is quoted that way.
LLM_SCHEMA_RETRIES = 1

#: Per-message and per-request framing tokens the estimate adds on top of the
#: text itself (role markers, the response-format wrapper).
MESSAGE_OVERHEAD_TOKENS = 16
REQUEST_OVERHEAD_TOKENS = 64

#: Input images per call and bytes per image (front-flow contract §6 bounds
#: what intake may fetch to 12 images of at most 10 MB each).
MAX_IMAGES_PER_CALL = 12
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_MILLION = Decimal(1_000_000)
_CENT_FRACTION = Decimal("0.000000000001")


class UnknownLlmModelError(LookupError):
    """No registry entry (and so no price) for this provider/model."""


@dataclass(frozen=True)
class LlmModelSpec:
    provider: str
    model: str
    display_name: str
    vision: bool
    input_per_mtok: Decimal
    cached_input_per_mtok: Decimal
    output_per_mtok: Decimal
    max_output_tokens: int
    image_input_tokens_ceiling: int
    #: Sent as ``reasoning_effort`` to reasoning models; ``None`` for others.
    reasoning_effort: str | None = None


def _openai(
    model: str,
    display_name: str,
    *,
    input_price: str,
    cached_price: str,
    output_price: str,
    image_ceiling: int,
    reasoning_effort: str | None,
) -> LlmModelSpec:
    return LlmModelSpec(
        provider=OPENAI_PROVIDER,
        model=model,
        display_name=display_name,
        vision=True,
        input_per_mtok=Decimal(input_price),
        cached_input_per_mtok=Decimal(cached_price),
        output_per_mtok=Decimal(output_price),
        max_output_tokens=16_384,
        image_input_tokens_ceiling=image_ceiling,
        reasoning_effort=reasoning_effort,
    )


#: Vision-capable, structured-output-capable chat models, cheapest useful
#: first. ``gpt-5-mini`` is the default: mini-class price, reads images,
#: honours strict JSON-schema output.
OPENAI_CHAT_MODELS: dict[str, LlmModelSpec] = {
    spec.model: spec
    for spec in (
        _openai(
            "gpt-5-mini", "GPT-5 mini",
            input_price="0.25", cached_price="0.025", output_price="2.00",
            image_ceiling=4_000, reasoning_effort="minimal",
        ),
        _openai(
            "gpt-5.4-mini", "GPT-5.4 mini",
            input_price="0.75", cached_price="0.075", output_price="4.50",
            image_ceiling=4_000, reasoning_effort="low",
        ),
        _openai(
            "gpt-5.4-nano", "GPT-5.4 nano",
            input_price="0.20", cached_price="0.02", output_price="1.25",
            image_ceiling=4_000, reasoning_effort="low",
        ),
        _openai(
            "gpt-4o-mini", "GPT-4o mini",
            input_price="0.15", cached_price="0.075", output_price="0.60",
            # 2,833 base + 6 tiles x 5,667 at high detail.
            image_ceiling=36_835, reasoning_effort=None,
        ),
    )
}
DEFAULT_OPENAI_CHAT_MODEL = "gpt-5-mini"

#: The controlled mock answers locally; it is priced like the default real
#: model only to show a *simulated* amount (never real spend, contract §10.3).
MOCK_CHAT_MODEL = "mock-llm-v1"
MOCK_CHAT_MODELS: dict[str, LlmModelSpec] = {
    MOCK_CHAT_MODEL: LlmModelSpec(
        provider=MOCK_PROVIDER,
        model=MOCK_CHAT_MODEL,
        display_name="Mock LLM v1",
        vision=True,
        input_per_mtok=OPENAI_CHAT_MODELS[DEFAULT_OPENAI_CHAT_MODEL].input_per_mtok,
        cached_input_per_mtok=OPENAI_CHAT_MODELS[DEFAULT_OPENAI_CHAT_MODEL].cached_input_per_mtok,
        output_per_mtok=OPENAI_CHAT_MODELS[DEFAULT_OPENAI_CHAT_MODEL].output_per_mtok,
        max_output_tokens=16_384,
        image_input_tokens_ceiling=4_000,
    ),
}

LLM_MODEL_REGISTRY: dict[str, dict[str, LlmModelSpec]] = {
    OPENAI_PROVIDER: OPENAI_CHAT_MODELS,
    MOCK_PROVIDER: MOCK_CHAT_MODELS,
}
DEFAULT_MODEL_BY_PROVIDER = {
    OPENAI_PROVIDER: DEFAULT_OPENAI_CHAT_MODEL,
    MOCK_PROVIDER: MOCK_CHAT_MODEL,
}


def model_spec(provider: str, model: str | None) -> LlmModelSpec:
    """The registry entry for ``provider``/``model`` (``None`` = provider default)."""
    models = LLM_MODEL_REGISTRY.get(provider)
    if models is None:
        raise UnknownLlmModelError(f"no chat models are registered for provider {provider!r}")
    resolved = model or DEFAULT_MODEL_BY_PROVIDER[provider]
    spec = models.get(resolved)
    if spec is None:
        raise UnknownLlmModelError(f"chat model {resolved!r} is not registered for {provider!r}")
    return spec


def estimate_text_tokens(text: str) -> int:
    """An upper bound on the tokens ``text`` encodes to.

    No tokenizer ships with the domain service, so the bound is the UTF-8
    byte count: a BPE token always covers at least one byte. It overstates
    English roughly fourfold, which only widens a reservation of fractions of
    a cent -- the settlement uses the provider's own count.
    """
    return len(text.encode("utf-8"))


def estimate_input_tokens(
    spec: LlmModelSpec,
    *,
    texts: tuple[str, ...],
    schema_json: str,
    image_count: int,
) -> int:
    """Upper bound on one request's input tokens: text, schema, images, framing."""
    return (
        sum(estimate_text_tokens(text) + MESSAGE_OVERHEAD_TOKENS for text in texts)
        + estimate_text_tokens(schema_json)
        + REQUEST_OVERHEAD_TOKENS
        + image_count * spec.image_input_tokens_ceiling
    )


def _per_token(price_per_mtok: Decimal) -> Decimal:
    return price_per_mtok / _MILLION


def request_max_cost(spec: LlmModelSpec, *, input_tokens: int, max_output_tokens: int) -> Decimal:
    """Most one provider request can cost: every input token uncached, every output token used."""
    amount = (
        Decimal(input_tokens) * _per_token(spec.input_per_mtok)
        + Decimal(max_output_tokens) * _per_token(spec.output_per_mtok)
    )
    return amount.quantize(_CENT_FRACTION, rounding=ROUND_CEILING)


def call_max_cost(spec: LlmModelSpec, *, input_tokens: int, max_output_tokens: int) -> Decimal:
    """Most one ``llm_chat`` call can cost, its schema retry included -- the quote."""
    return request_max_cost(
        spec, input_tokens=input_tokens, max_output_tokens=max_output_tokens,
    ) * (1 + LLM_SCHEMA_RETRIES)


def usage_cost(spec: LlmModelSpec, usage: Mapping[str, int]) -> Decimal:
    """What reported usage costs at list price (cached input billed at its own rate)."""
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    cached = min(int(usage.get("cached_input_tokens", 0) or 0), input_tokens)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    amount = (
        Decimal(input_tokens - cached) * _per_token(spec.input_per_mtok)
        + Decimal(cached) * _per_token(spec.cached_input_per_mtok)
        + Decimal(output_tokens) * _per_token(spec.output_per_mtok)
    )
    return amount.quantize(_CENT_FRACTION, rounding=ROUND_CEILING)


def estimate_output_tokens(text: str) -> int:
    """A deterministic output-token figure for the mock (about 4 bytes a token)."""
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


def describe_models(provider: str) -> dict[str, dict]:
    """The ``capabilities`` view of a provider's chat models (prices as strings)."""
    return {
        model: {
            "display_name": spec.display_name,
            "vision": spec.vision,
            "max_output_tokens": spec.max_output_tokens,
            "pricing": {
                "currency": "USD",
                "unit": "1M tokens",
                "input": str(spec.input_per_mtok),
                "cached_input": str(spec.cached_input_per_mtok),
                "output": str(spec.output_per_mtok),
            },
        }
        for model, spec in LLM_MODEL_REGISTRY.get(provider, {}).items()
    }
