"""Centralized execution-mode policy module.

Concentrates mode-specific credential, Adapter, cost, and admission
rules. Callers never scatter `if mode == ...` branches.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class PreviewResolution:
    effective_mode: str
    adapter_key: str
    adapter_version: str
    capabilities: dict
    credential_evidence: dict
    cost_policy: dict
    scenario_evidence: dict
    #: Whether this Run sends the product's reference images, and how many
    #: (``app.generation.creative_recipe.decide_reference_use``); frozen into
    #: the recipe and shown as the ``reference_images`` capability decision.
    reference_decision: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PreviewRejection:
    code: str
    detail: str


@dataclass(frozen=True)
class StepAdmission:
    adapter_key: str
    credential_handle: str | None
    cost_admission: dict
    redacted_call_policy: dict


@dataclass(frozen=True)
class StepRejection:
    code: str
    detail: str
    recoverable: bool


def resolve_preview(
    session: Session,
    *,
    draft_mode: str,
    provider: str,
    model: str,
    adapter_registry: dict,
    credential_evidence: dict | None = None,
    scenario_id: str | None = None,
    format_config: dict | None = None,
    reference_images_available: int = 0,
) -> PreviewResolution | PreviewRejection:
    from app.generation.creative_recipe import decide_reference_use

    if draft_mode == "mock":
        if "mock" not in adapter_registry:
            return PreviewRejection(
                code="mode_unavailable",
                detail="No MockProviderAdapter registered",
            )
        return PreviewResolution(
            effective_mode="mock",
            adapter_key="mock",
            adapter_version=adapter_registry["mock"].get("version", "v1"),
            capabilities=adapter_registry["mock"].get("capabilities", {}),
            credential_evidence={},
            cost_policy={"true_payable": "zero"},
            scenario_evidence={
                "scenario_id": scenario_id or "default_success",
            },
            reference_decision=decide_reference_use(
                adapter_registry["mock"], model, reference_images_available, execution_mode="mock",
            ),
        )
    elif draft_mode == "byok":
        if not credential_evidence or not credential_evidence.get("active"):
            return PreviewRejection(
                code="credential_required",
                detail="BYOK mode requires an active eligible CredentialVersion",
            )
        adapter_info = adapter_registry.get(provider)
        if not adapter_info or not adapter_info.get("advertised"):
            return PreviewRejection(
                code="mode_unavailable",
                detail=f"No BYOK adapter advertised for {provider}",
            )
        if model not in (adapter_info.get("models") or {}):
            return PreviewRejection(
                code="model_unavailable",
                detail=f"Model {model!r} is not offered by {provider}",
            )
        reference_decision = decide_reference_use(
            adapter_info, model, reference_images_available, execution_mode="byok",
        )
        return PreviewResolution(
            effective_mode="byok",
            adapter_key=provider,
            adapter_version=adapter_info.get("version", "v1"),
            capabilities=adapter_info.get("capabilities", {}),
            credential_evidence=credential_evidence,
            cost_policy=_byok_cost_policy(
                adapter_info, provider, model, format_config,
                reference_images=int(reference_decision.get("count") or 0),
            ),
            scenario_evidence={},
            reference_decision=reference_decision,
        )
    elif draft_mode == "sandbox":
        adapter_info = adapter_registry.get(f"{provider}_sandbox")
        if not adapter_info or not adapter_info.get("advertised"):
            return PreviewRejection(
                code="mode_unavailable",
                detail=f"No sandbox adapter advertised for {provider}",
            )
        return PreviewResolution(
            effective_mode="sandbox",
            adapter_key=f"{provider}_sandbox",
            adapter_version=adapter_info.get("version", "v1"),
            capabilities=adapter_info.get("capabilities", {}),
            credential_evidence=credential_evidence or {},
            cost_policy=adapter_info.get("cost_policy", {"true_payable": "zero"}),
            scenario_evidence={},
            reference_decision=decide_reference_use(
                adapter_info, model, reference_images_available, execution_mode="sandbox",
            ),
        )
    return PreviewRejection(
        code="invalid_mode",
        detail=f"Unknown execution mode: {draft_mode}",
    )


def _byok_cost_policy(
    adapter_info: dict, provider: str, model: str, format_config: dict | None,
    *, reference_images: int = 0,
) -> dict:
    """The quote the preview shows for a real provider call.

    ``estimated_per_call`` is the list price of one output image at the
    Draft's format (``app.generation.pricing``); the preview multiplies it by
    the call count and Atomic Submission reserves the same amount per item.
    A call that sends ``reference_images`` product images is priced as such
    (image edits / input images). A provider without a list price still
    resolves -- its cost simply cannot be quoted ahead of time.
    """
    from app.generation import pricing

    policy = {"true_payable": "provider_quoted"}
    policy.update(adapter_info.get("cost_policy") or {})
    policy["true_payable"] = "provider_quoted"
    try:
        policy["estimated_per_call"] = str(
            pricing.unit_price(provider, model, format_config, reference_images=reference_images)
        )
    except pricing.UnpricedCallError:
        policy.setdefault("estimated_per_call", "0")
    policy.setdefault("currency", "USD")
    policy["price_rule_version"] = pricing.PRICE_RULE_VERSION
    policy["reference_images_per_call"] = reference_images
    return policy


def admit_external_step(
    session: Session,
    *,
    snapshot_mode: str,
    adapter_registry: dict,
    credential_evidence: dict | None = None,
) -> StepAdmission | StepRejection:
    if snapshot_mode == "mock":
        return StepAdmission(
            adapter_key="mock",
            credential_handle=None,
            cost_admission={"true_payable": "zero", "guardrail_effect": "none"},
            redacted_call_policy={},
        )
    elif snapshot_mode == "byok":
        if not credential_evidence or not credential_evidence.get("active"):
            return StepRejection(
                code="credential_unavailable",
                detail="BYOK credential no longer active",
                recoverable=True,
            )
        return StepAdmission(
            adapter_key=credential_evidence.get("provider", ""),
            credential_handle=credential_evidence.get("version_id"),
            cost_admission={"true_payable": "provider_quoted"},
            redacted_call_policy={},
        )
    elif snapshot_mode == "sandbox":
        adapter_key = f"{credential_evidence.get('provider', '')}_sandbox" if credential_evidence else ""
        if adapter_key not in adapter_registry:
            return StepRejection(
                code="mode_unavailable",
                detail="Sandbox adapter no longer available",
                recoverable=False,
            )
        return StepAdmission(
            adapter_key=adapter_key,
            credential_handle=None,
            cost_admission={"true_payable": "zero"},
            redacted_call_policy={},
        )
    return StepRejection(
        code="invalid_mode",
        detail=f"Unknown execution mode: {snapshot_mode}",
        recoverable=False,
    )
