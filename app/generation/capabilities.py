"""Workspace-scoped Generation capabilities lookup.

Resolves which providers, models, execution modes, and format options are
currently available for a Workspace based on:

1. Platform adapter registry (which provider adapters are deployed).
2. BYOK credential readiness (which providers have an active credential).
3. Execution-policy admission rules (consistent with ``execution_policy.py``).

The response is cacheable (the router sets an ETag derived from the response
content hash).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entitlements.credentials import CredentialBindingRecord, CredentialVersionRecord
from app.platform.tenancy import TenantContext
from app.generation.selection import (
    ASPECT_RATIOS, IMAGE_SIZES, MAX_PROMPT_LENGTH, MAX_VARIANTS,
    product_options, provider_catalog,
)


PLATFORM_ADAPTER_REGISTRY: dict = {
    "mock": {
        "version": "v1",
        "capabilities": {"image_generation": True},
        "advertised": True,
        "models": {
            "mock-v1": {
                "display_name": "Mock Generator v1",
                "format_limits": {
                    "max_width": 2048,
                    "max_height": 2048,
                    "aspect_ratios": ["1:1", "4:3", "16:9", "9:16"],
                    "max_variants_per_combination": 4,
                },
            },
        },
    },
    "meta": {
        "version": "v1",
        "capabilities": {"image_generation": True},
        "advertised": True,
        "models": {
            "llama-image-v1": {
                "display_name": "Llama Image v1",
                "format_limits": {
                    "max_width": 2048,
                    "max_height": 2048,
                    "aspect_ratios": ["1:1", "4:3", "16:9", "9:16", "3:4"],
                    "max_variants_per_combination": 4,
                },
            },
        },
    },
    "meta_sandbox": {
        "version": "v1",
        "capabilities": {"image_generation": True},
        "advertised": True,
        "cost_policy": {"true_payable": "zero"},
        "models": {
            "llama-image-v1": {
                "display_name": "Llama Image v1 (Sandbox)",
                "format_limits": {
                    "max_width": 1024,
                    "max_height": 1024,
                    "aspect_ratios": ["1:1", "4:3", "16:9"],
                    "max_variants_per_combination": 2,
                },
            },
        },
    },
    # OpenAI GPT-Image, dispatched by ``app.generation.openai_adapter`` under
    # ``byok`` only: the Workspace binds its own OpenAI key
    # (``binding_scope="generation"``), the preview quotes ``Maximum cost``
    # from ``app.generation.pricing``, and Atomic Submission reserves it.
    # GPT-Image renders 1024x1024, 1024x1536 and 1536x1024; the ratios below
    # are the ones ``pricing.OPENAI_SIZE_FOR_ASPECT_RATIO`` snaps to them.
    # Every GPT-Image model takes reference images through
    # ``/v1/images/edits`` (``supports_reference_images``, Issue #167).
    "openai": {
        "version": "v1",
        "capabilities": {"image_generation": True},
        "advertised": True,
        "cost_policy": {"true_payable": "provider_quoted", "currency": "USD"},
        "models": {
            model_id: {
                "display_name": display_name,
                "format_limits": {
                    "max_width": 1536,
                    "max_height": 1536,
                    "aspect_ratios": ["1:1", "4:5", "9:16", "2:3", "3:4", "16:9", "3:2", "4:3"],
                    "max_variants_per_combination": 4,
                },
                "supports_reference_images": True,
                "max_reference_images": 4,
            }
            for model_id, display_name in (
                ("gpt-image-2", "GPT Image 2"),
                ("gpt-image-1.5", "GPT Image 1.5"),
                ("gpt-image-1", "GPT Image 1"),
                ("gpt-image-1-mini", "GPT Image 1 mini"),
            )
        },
    },
    # Google Gemini image (``generateContent``), dispatched by
    # ``app.generation.gemini_adapter`` under ``byok`` with the Workspace's own
    # Gemini API key bound as provider ``gemini``. Advertised only once that
    # key is bound (``advertise_requires_credential``): there is no Mock
    # stand-in and the Settings page offers OpenAI first.
    "gemini": {
        "version": "v1",
        "capabilities": {"image_generation": True},
        "advertised": True,
        "advertise_requires_credential": True,
        "cost_policy": {"true_payable": "provider_quoted", "currency": "USD"},
        "models": {
            "gemini-3.1-flash-image": {
                "display_name": "Gemini 3.1 Flash Image",
                "format_limits": {
                    "max_width": 1536,
                    "max_height": 1536,
                    "aspect_ratios": ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"],
                    "max_variants_per_combination": 4,
                },
                "supports_reference_images": True,
                "max_reference_images": 4,
            },
        },
    },
    # ByteDance Seedream on Volcengine Ark (``/images/generations``),
    # dispatched by ``app.generation.seedream_adapter`` under ``byok`` with the
    # Workspace's own Ark API key bound as provider ``seedream``; advertised
    # only once that key is bound. Seedream 4.5 renders at the 2K sizes of
    # ``app.providers.seedream.SEEDREAM_SIZES_2K``.
    "seedream": {
        "version": "v1",
        "capabilities": {"image_generation": True},
        "advertised": True,
        "advertise_requires_credential": True,
        "cost_policy": {"true_payable": "provider_quoted", "currency": "USD"},
        "models": {
            "doubao-seedream-4-5-251128": {
                "display_name": "Seedream 4.5",
                "format_limits": {
                    "max_width": 2560,
                    "max_height": 2560,
                    "aspect_ratios": ["1:1", "2:3", "3:2", "3:4", "4:5", "9:16", "16:9"],
                    "max_variants_per_combination": 4,
                },
                "supports_reference_images": True,
                "max_reference_images": 4,
            },
        },
    },
}

KNOWN_EXECUTION_MODES = ["mock", "sandbox", "byok"]


def get_capabilities(
    session: Session,
    ctx: TenantContext,
    *,
    adapter_registry: dict | None = None,
) -> dict:
    """Return the resolved Generation capabilities for the current Workspace.

    The result is a ``GenerationCapabilities`` dict whose shape matches
    ``response_models.GenerationCapabilities``.
    """
    registry = adapter_registry if adapter_registry is not None else PLATFORM_ADAPTER_REGISTRY

    credential_readiness = _query_credential_readiness(session, ctx)

    providers = _resolve_providers(registry, credential_readiness)
    models = _resolve_models(registry, providers)
    modes = _resolve_modes(registry, credential_readiness)
    format_limits = _resolve_format_limits(registry, providers)

    # The governed ``llm_chat`` semantic (Issue #160): its models, prices and
    # whether this Workspace's bound OpenAI key makes ``byok`` chat ready.
    from app.providers.llm_chat import llm_capability

    return {
        "providers": providers,
        "models": models,
        "modes": modes,
        "format_limits": format_limits,
        "credential_readiness": credential_readiness,
        "llm": llm_capability(session, ctx),
        "provider_options": __import__("app.generation.selection", fromlist=["provider_catalog"]).provider_catalog(session, ctx),
        "products": __import__("app.generation.selection", fromlist=["product_options"]).product_options(session, ctx),
    }


def _query_credential_readiness(session: Session, ctx: TenantContext) -> dict:
    """Check which providers have an active BYOK credential bound in this Workspace."""
    bindings = session.execute(
        select(
            CredentialBindingRecord.provider,
            CredentialBindingRecord.credential_version_id,
            CredentialVersionRecord.state,
        )
        .join(
            CredentialVersionRecord,
            (CredentialBindingRecord.credential_version_id == CredentialVersionRecord.id)
            & (CredentialBindingRecord.workspace_id == CredentialVersionRecord.workspace_id),
        )
        .where(
            CredentialBindingRecord.workspace_id == ctx.workspace_id,
            CredentialBindingRecord.binding_scope == "generation",
        )
    ).all()

    readiness: dict = {}
    for provider, version_id, state in bindings:
        readiness[provider] = {
            "has_active_credential": state == "active",
            "credential_version_id": version_id if state == "active" else None,
        }
    return readiness


def _resolve_providers(registry: dict, credential_readiness: dict | None = None) -> list[str]:
    """Extract advertised provider names (excluding sandbox variants).

    A provider marked ``advertise_requires_credential`` (Gemini, Seedream) is
    listed only while this Workspace has an active key bound for it.
    """
    readiness = credential_readiness or {}
    providers = []
    for key, info in registry.items():
        if key.endswith("_sandbox"):
            continue
        if not info.get("advertised", False):
            continue
        if info.get("advertise_requires_credential") and not (
            readiness.get(key) or {}
        ).get("has_active_credential"):
            continue
        providers.append(key)
    return sorted(providers)


def _resolve_models(registry: dict, providers: list[str]) -> dict:
    """Build provider → model list mapping from the registry."""
    result: dict = {}
    for provider in providers:
        adapter_info = registry.get(provider, {})
        models = adapter_info.get("models", {})
        result[provider] = {
            model_id: {
                "display_name": model_info.get("display_name", model_id),
                "format_limits": model_info.get("format_limits", {}),
                "supports_reference_images": bool(model_info.get("supports_reference_images", False)),
                "max_reference_images": int(model_info.get("max_reference_images") or 0),
            }
            for model_id, model_info in models.items()
        }
    return result


def _resolve_modes(registry: dict, credential_readiness: dict) -> list[str]:
    """Determine which execution modes are currently available.

    Mirrors the admission logic in ``execution_policy.resolve_preview``:
    - ``mock`` is available if a mock adapter is registered.
    - ``sandbox`` is available if any provider has an advertised sandbox adapter.
    - ``byok`` is available if any provider has an active credential.
    """
    modes = []

    if "mock" in registry:
        modes.append("mock")

    has_sandbox = any(
        key.endswith("_sandbox") and info.get("advertised", False)
        for key, info in registry.items()
    )
    if has_sandbox:
        modes.append("sandbox")

    has_active_credential = any(
        info.get("has_active_credential", False)
        for info in credential_readiness.values()
    )
    if has_active_credential:
        modes.append("byok")

    return modes


def max_variants_per_combination(
    registry: dict, adapter_key: str, model: str
) -> int | None:
    """The variant ceiling the resolved adapter advertises for ``model``.

    Looks up the exact model's ``format_limits.max_variants_per_combination``
    (``adapter_key`` is the *resolved* adapter -- ``meta_sandbox`` for a
    sandbox preview, so the sandbox's tighter limit applies). An adapter that
    does not list the model falls back to its widest model limit; an unknown
    adapter or one with no limits returns ``None`` (no ceiling known).
    Preview and Atomic Submission enforce this (Issue #157) so the page's
    variant count can never exceed what ``GET /generation/capabilities``
    advertises.
    """
    adapter_info = registry.get(adapter_key) or {}
    models = adapter_info.get("models") or {}
    model_limits = (models.get(model) or {}).get("format_limits") or {}
    limit = model_limits.get("max_variants_per_combination")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        return limit
    widest = 0
    for model_info in models.values():
        candidate = (model_info.get("format_limits") or {}).get("max_variants_per_combination", 0)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            widest = max(widest, candidate)
    return widest or None


def _resolve_format_limits(registry: dict, providers: list[str]) -> dict:
    """Aggregate format limits across all models for each provider."""
    result: dict = {}
    for provider in providers:
        adapter_info = registry.get(provider, {})
        models = adapter_info.get("models", {})
        all_ratios: set[str] = set()
        max_width = 0
        max_height = 0
        max_variants = 0
        for model_info in models.values():
            limits = model_info.get("format_limits", {})
            all_ratios.update(limits.get("aspect_ratios", []))
            max_width = max(max_width, limits.get("max_width", 0))
            max_height = max(max_height, limits.get("max_height", 0))
            max_variants = max(max_variants, limits.get("max_variants_per_combination", 0))
        result[provider] = {
            "max_width": max_width,
            "max_height": max_height,
            "aspect_ratios": sorted(all_ratios),
            "max_variants_per_combination": max_variants,
        }
    return result
