"""AI suggestions remain distinct from the human quality decision."""
from typing import Literal
from pydantic import Field
from .contracts import Document, Text


class AICheckFinding(Document):
    check_id: str = Field(min_length=1, max_length=64)
    status: Literal["pass", "fail", "unknown"]
    confidence: Literal["high", "medium", "low"]
    visual_evidence: str = Field(min_length=1, max_length=320)
    rationale: str = Field(min_length=1, max_length=320)
    suggested_fix: str = Field(max_length=320)
    fact_ids: list[str] = Field(max_length=10)


class AICheckReport(Document):
    summary: str = Field(min_length=1, max_length=1000)
    findings: list[AICheckFinding] = Field(min_length=1, max_length=30)


class RenderedText(Document):
    """The headline and body text as the image renders them, transcribed verbatim; empty when not shown or unreadable."""
    headline: str = Field(max_length=2000)
    body_copy: str = Field(max_length=2000)


class AICheckTranscribedReport(Document):
    """Single-image QC from v3 (#294): the model transcribes the rendered copy first, the server compares it."""
    rendered_text: RenderedText
    summary: str = Field(min_length=1, max_length=1000)
    findings: list[AICheckFinding] = Field(min_length=1, max_length=30)


# Single-image policies whose output carries `rendered_text`; earlier calls keep the report they were asked for.
TRANSCRIBED_POLICIES = frozenset({"marketing-vision-qc-single-v3"})


def report_model(inputs):
    """The report a call's model output must match, by the policy frozen into its input."""
    return AICheckTranscribedReport if inputs.get("policy_version") in TRANSCRIBED_POLICIES else AICheckReport


QCKind = Literal["single", "paired"]
StrategyLanguage = Literal["zh-CN", "en-US"]


class StartAIQC(Document):
    quote_hash: str = Field(min_length=64, max_length=64)
    # Each kind is quoted, reserved and settled on its own; clients from before the split run single checks.
    kind: QCKind = "single"
    # The requester's interface language, used only when the round's plan has no strategy_language (J-43).
    strategy_language: StrategyLanguage | None = None


class ConfirmAIQC(Document):
    ai_qc_ids: list[str] = Field(min_length=1, max_length=60)
    # Shared human confirmation applies only to common unresolved checks,
    # never to different angle-specific intent_n checks or an AI failure.
    resolved_checks: dict[str, Literal["pass"]] = Field(default_factory=dict, max_length=6)
    note: str = Field(default="", max_length=2000)


class PairedQCWaiting(Document):
    """An image whose paired check waits for usable images of these counterpart combinations."""
    asset_version_id: str
    awaiting_combination_ids: list[str]


class AIQCQuote(Document):
    available: bool
    unavailable_reason: str | None
    provider: str
    model: str
    execution_mode: Literal["byok", "mock"]
    quantity: int
    maximum_cost: str
    currency: Literal["USD"] = "USD"
    quote_hash: str
    asset_version_ids: list[str]
    # Present when the request names a kind.
    kind: QCKind | None = None
    waiting: list[PairedQCWaiting] | None = None
