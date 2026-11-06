"""Tag an external creative by dimension value with the governed ``llm_chat``.

One vision call per image: the model sees the gate's controlled derivative
(EXIF-free JPEG -- contract §7.2, providers only read controlled
derivatives) and the product's frozen vocabulary, and answers, per factor,
the single best value or ``none`` ("none of these"), a confidence and a
short reason. The answer is a suggestion; nothing counts until the user
confirms (contract §6 "AI 提议不等于确认").

The JSON schema is built per request from the vocabulary: factor keys are
``f1`` .. ``fN`` and each factor's ``value_id`` is an ``enum`` of that
factor's own values plus ``none``, so a structured-output model cannot
answer a value that does not exist. The request is a pure function of the
frozen vocabulary (plus the one image), so admission can reserve the exact
input ceiling the Worker later sends (:func:`build_request`).

Controlled Mock (``asset_tagging``): a deterministic, schema-valid example
-- each factor gets the value picked by the image's SHA-256, confidence
between 0.55 and 0.9, and a reason that says it is a Mock.
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.providers.llm_mock import register_llm_mock_fixture, synthesize_from_schema
from app.providers.llm_types import LlmChatRequest, LlmImage, ResolvedLlmChatRequest

SEMANTIC = "asset_tagging"
SCHEMA_NAME = "asset_tagging"
CALL_NAME = "tag"
NONE = "none"
MAX_OUTPUT_TOKENS = 3_000
MAX_REASON_CHARS = 160
#: A suggestion below this confidence is shown but not pre-selected.
PRESELECT_CONFIDENCE = 0.5

SYSTEM_PROMPT = (
    "You are a meticulous creative analyst for performance advertising. You look at one "
    "finished ad image that was made outside our platform (by a design tool, a designer or "
    "a real customer) and classify it against the advertiser's own test vocabulary: for each "
    "factor, pick the single value that best describes what is visibly in the image. If no "
    "value fits, or the image gives no evidence either way, answer \"none\" -- never guess. "
    "Confidence is your probability (0..1) that the chosen value is right; use low values "
    "when the evidence is weak. Reasons are one short sentence in Simplified Chinese that "
    "names the visible evidence. Answer only with JSON matching the schema."
)


def factor_key(index: int) -> str:
    return f"f{index + 1}"


def build_user_prompt(vocabulary: dict) -> str:
    lines = ["Classify the attached ad image against these factors.", ""]
    for index, factor in enumerate(vocabulary.get("factors") or []):
        lines.append(f"{factor_key(index)}: {factor['label']}")
        for value in factor.get("values") or []:
            lines.append(f"  - {value['value_id']}: {value['label']}")
            if value.get("guidance"):
                lines.append(f"    how to tell: {value['guidance']}")
        lines.append(f"  - {NONE}: 以上都不是 / 看不出来")
    lines.append("")
    lines.append("For every factor give value_id, confidence and reason; then a one-sentence summary of the image.")
    return "\n".join(lines)


def build_schema(vocabulary: dict) -> dict:
    properties: dict[str, Any] = {}
    for index, factor in enumerate(vocabulary.get("factors") or []):
        properties[factor_key(index)] = {
            "type": "object",
            "properties": {
                "value_id": {
                    "type": "string",
                    "enum": [value["value_id"] for value in factor.get("values") or []] + [NONE],
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string", "maxLength": MAX_REASON_CHARS},
            },
            "required": ["value_id", "confidence", "reason"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "factors": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
            "summary": {"type": "string", "maxLength": 300},
        },
        "required": ["factors", "summary"],
        "additionalProperties": False,
    }


def build_request(vocabulary: dict, image: LlmImage) -> LlmChatRequest:
    return LlmChatRequest.build(
        semantic=SEMANTIC,
        system=SYSTEM_PROMPT,
        user=build_user_prompt(vocabulary),
        json_schema=build_schema(vocabulary),
        schema_name=SCHEMA_NAME,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        images=(image,),
    )


#: Admission builds the request before the gate derived the image: only the
#: image *count* enters the input-token ceiling, never its bytes.
PLACEHOLDER_IMAGE = LlmImage.from_bytes(b"", "image/jpeg")


def normalize(data: dict, vocabulary: dict) -> dict:
    """The model's answer as ``{factors: [{factor_id, value_id|None, confidence, reason}], summary}``.

    Schema validation already happened in ``llm_chat``; this maps the
    request-local ``f1`` keys back to vocabulary factor ids, turns ``none``
    into ``None``, clamps confidence and trims reasons -- defensively, since
    the vocabulary is frozen and the enum already bounds every value.
    """
    answers = dict((data or {}).get("factors") or {})
    factors: list[dict] = []
    for index, factor in enumerate(vocabulary.get("factors") or []):
        raw = answers.get(factor_key(index)) or {}
        value_id = raw.get("value_id")
        known = {value["value_id"] for value in factor.get("values") or []}
        if value_id not in known:
            value_id = None
        try:
            confidence = float(raw.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = round(min(1.0, max(0.0, confidence)), 3)
        factors.append({
            "factor_id": factor["factor_id"],
            "value_id": value_id,
            "confidence": confidence,
            "reason": " ".join(str(raw.get("reason") or "").split())[:MAX_REASON_CHARS],
        })
    return {
        "factors": factors,
        "summary": " ".join(str((data or {}).get("summary") or "").split())[:300],
    }


# ---------------------------------------------------------------------------
# Controlled Mock
# ---------------------------------------------------------------------------


def _mock_asset_tagging(request: ResolvedLlmChatRequest) -> dict:
    """Deterministic per image: the image hash picks each factor's value."""
    if request.schema_name != SCHEMA_NAME:
        return synthesize_from_schema(request.json_schema)
    seed = request.images[0].sha256 if request.images else request.request_digest
    properties = request.json_schema["properties"]["factors"]["properties"]
    factors = {}
    for key, schema in properties.items():
        options = list(schema["properties"]["value_id"]["enum"])
        picks = [option for option in options if option != NONE] or [NONE]
        digest = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).digest()
        value_id = picks[digest[0] % len(picks)]
        confidence = round(0.55 + (digest[1] % 36) / 100, 2)
        factors[key] = {
            "value_id": value_id,
            "confidence": confidence,
            "reason": "受控 Mock：未调用模型，按图片指纹给出的示例标签，请人工确认。",
        }
    return {"factors": factors, "summary": "受控 Mock 打标示例（不是模型判断）。"}


register_llm_mock_fixture(SEMANTIC, _mock_asset_tagging)
