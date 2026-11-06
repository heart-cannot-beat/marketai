"""Controlled Mock for ``llm_chat``: deterministic answers, zero real cost.

The mock never leaves the process. It answers by ``semantic`` tag:

1. a fixture registered for the tag (:func:`register_llm_mock_fixture`) --
   a dict, or a callable of the resolved request returning a dict -- the
   way a caller (product intake, plan drafting, asset tagging, QC) ships a
   realistic example with its own schema;
2. otherwise an instance synthesized from the request's JSON schema
   (:func:`synthesize_from_schema`), so any caller works under Mock before
   it registers a fixture.

Either way the answer is validated against the request's schema exactly
like a real model's (a fixture that drifts from its schema fails loudly as
``mock_fixture_invalid`` instead of feeding a caller malformed data), the
usage it reports is a deterministic function of the request, and its cost
is a *simulated* amount only -- ``provider_amount`` is always zero and no
reservation exists for a mock call (contract §10.3).
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable, Mapping, Union

from app.providers import json_schema
from app.providers.adapter import (
    ProviderCapabilities,
    ProviderDispatchError,
    ProviderResult,
    ReportedCost,
)
from app.providers.llm_registry import (
    LLM_PRICE_RULE_VERSION,
    MOCK_PROVIDER,
    estimate_output_tokens,
    model_spec,
    usage_cost,
)
from app.providers.llm_types import LlmChatOutput, LlmOutputValidationError, ResolvedLlmChatRequest

MockFixture = Union[Mapping[str, Any], Callable[[ResolvedLlmChatRequest], Mapping[str, Any]]]

_FIXTURES: dict[str, MockFixture] = {}


def register_llm_mock_fixture(semantic: str, fixture: MockFixture) -> None:
    """Make the mock answer ``semantic`` requests with ``fixture``.

    Registering the same tag again replaces the fixture (tests and callers
    own their tags). The fixture must satisfy the schema its caller sends.
    """
    if not semantic:
        raise ValueError("a mock fixture needs a semantic tag")
    _FIXTURES[semantic] = fixture


def unregister_llm_mock_fixture(semantic: str) -> None:
    _FIXTURES.pop(semantic, None)


def registered_mock_semantics() -> tuple[str, ...]:
    return tuple(sorted(_FIXTURES))


# ---------------------------------------------------------------------------
# Example fixture (the pattern callers copy for their own semantic)
# ---------------------------------------------------------------------------

#: ``example.summary`` -- a generic "summarize what you were given" answer.
#: Callers copy this pattern: export the schema they send, register a
#: fixture that satisfies it, and the controlled mock answers their tag.
EXAMPLE_SUMMARY_SEMANTIC = "example.summary"
EXAMPLE_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "highlights": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 6},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "language": {"type": "string", "enum": ["zh", "en"]},
    },
    "required": ["title", "highlights", "confidence", "language"],
    "additionalProperties": False,
}


def _example_summary(request: ResolvedLlmChatRequest) -> dict[str, Any]:
    user_text = next(
        (message.text for message in reversed(request.messages) if message.role == "user"), "",
    )
    title = (user_text.strip().splitlines() or ["(empty)"])[0][:60]
    return {
        "title": f"[mock] {title}",
        "highlights": [
            "Controlled Mock answer — not produced by a model",
            f"{len(request.images)} image(s) attached",
        ],
        "confidence": 0.5,
        "language": "en",
    }


register_llm_mock_fixture(EXAMPLE_SUMMARY_SEMANTIC, _example_summary)


# ---------------------------------------------------------------------------
# ``test_plan_draft`` (Issue #164)
# ---------------------------------------------------------------------------

#: The governed plan drafter's tag and schema name
#: (``app.plans.llm_drafter.SEMANTIC`` / ``SCHEMA_NAME``).
TEST_PLAN_DRAFT_SEMANTIC = "test_plan_draft"
TEST_PLAN_DRAFT_SCHEMA_NAME = "test_plan_draft"


def _value(label: str, positive: list[str], negative: list[str]) -> dict[str, Any]:
    return {"label": label, "positive_fragments": positive, "negative_fragments": negative}


def _uses(**factor_values: list[str]) -> list[dict[str, Any]]:
    return [{"factor": factor, "values": values} for factor, values in factor_values.items()]


#: A realistic answer for the EV-charger product the intake mock produces:
#: 人物 有人/无人 x 场景 工业/生活/产品 x 汽车 出车/不出车, plus the 钩子
#: factor every draft carries. Four directions, ten combinations in all.
TEST_PLAN_DRAFT_EXAMPLE: dict[str, Any] = {
    "factors": [
        {
            "key": "people", "label": "人物", "is_hook": False,
            "values": [
                _value("有人", ["a car owner in their 30s plugging the charger into an electric car, candid moment"],
                       ["stiff posed model", "distorted hands"]),
                _value("无人", ["no people in frame, product-focused composition"],
                       ["people", "hands"]),
            ],
        },
        {
            "key": "scene", "label": "场景", "is_hook": False,
            "values": [
                _value("工业", ["wall-mounted on a concrete parking garage pillar, industrial lighting"],
                       ["cluttered background", "dirty walls"]),
                _value("生活", ["mounted in a tidy private home garage at dusk, warm ambient light"],
                       ["messy garage", "dark underexposed scene"]),
                _value("产品", ["studio shot on a seamless light grey backdrop, soft box lighting"],
                       ["busy background", "harsh shadows"]),
            ],
        },
        {
            "key": "car", "label": "汽车", "is_hook": False,
            "values": [
                _value("出车", ["a modern electric car parked beside the charger with the cable plugged in"],
                       ["visible car brand logo", "petrol car"]),
                _value("不出车", ["no car in frame, the charger alone with its coiled cable"],
                       ["car", "vehicle"]),
            ],
        },
        {
            "key": "hook", "label": "钩子", "is_hook": True,
            "values": [
                _value("一晚充满", ["dashboard showing 100% charge at sunrise, overnight charging done"],
                       ["low battery warning"]),
                _value("谷电省钱", ["phone app scheduling off-peak night charging, calm night mood"],
                       ["cash or coins", "price tags"]),
                _value("安全耐用", ["rain droplets on the IP55 housing, rugged reliable build"],
                       ["sparks", "exposed wires"]),
            ],
        },
    ],
    "directions": [
        {
            "title": "回家插上，一晚充满",
            "what_it_tests": "车主在自家车库插枪的生活画面，是否比纯产品图更能让新能源车主点击。",
            "hypothesis": "看到和自己一样的车主在家充电，会比参数图更快建立“我也需要”的代入感。",
            "scene_description": "傍晚的私家车库，车主把枪头插进电动车，墙上的充电桩环形灯亮起。",
            "copy": {"headline": "回家插上，早上满电", "primary_text": "7kW 家用充电桩，比随车充快 3 倍，一晚充满不用再排队找公共桩。"},
            "factor_values": _uses(人物=["有人"], 场景=["生活"], 汽车=["出车", "不出车"], 钩子=["一晚充满"]),
            "exploratory": False,
            "positive_fragments": ["warm lifestyle advertising photograph, home charging routine"],
            "negative_fragments": ["unreadable text", "distorted charger shape"],
        },
        {
            "title": "谷电时段充，电费更省",
            "what_it_tests": "“省电费”的钩子是否比“充得快”更能打动注重成本的通勤族。",
            "hypothesis": "把 App 预约谷电和夜间充电放进画面，会让通勤族直观感到每公里成本更低。",
            "scene_description": "夜里的家庭车库，手机 App 显示已预约谷电充电，电动车安静地充着电。",
            "copy": {"headline": "谷电充电，每公里更省", "primary_text": "App 预约谷电时段自动开始充电，白天高峰电价与你无关。"},
            "factor_values": _uses(人物=["有人", "无人"], 场景=["生活"], 汽车=["出车"], 钩子=["谷电省钱"]),
            "exploratory": False,
            "positive_fragments": ["night-time home garage, calm blue ambient light"],
            "negative_fragments": ["unreadable text", "cluttered phone screen"],
        },
        {
            "title": "车位在室外也能装",
            "what_it_tests": "强调 IP55 防护的工业场景，能否打消车位在室外的车主对安全的顾虑。",
            "hypothesis": "雨水中依然稳定工作的充电桩画面，会比文字参数更能证明安全耐用。",
            "scene_description": "露天或地下停车场的立柱上挂着充电桩，表面有雨滴，环形灯稳定亮着。",
            "copy": {"headline": "IP55 防护，风雨无忧", "primary_text": "室外车位也能放心装，动态负载均衡不跳闸。"},
            "factor_values": _uses(人物=["无人"], 场景=["工业", "产品"], 汽车=["不出车"], 钩子=["安全耐用"]),
            "exploratory": True,
            "positive_fragments": ["rugged product photograph, rain and concrete textures"],
            "negative_fragments": ["sparks", "exposed wires", "rust"],
        },
        {
            "title": "质感特写，一眼看懂",
            "what_it_tests": "干净的产品质感特写能否单靠外观和卖点标注拿到点击。",
            "hypothesis": "哑光黑外壳与环形灯的特写能传达高品质，适合首次了解家用充电桩的人。",
            "scene_description": "浅灰无缝背景上的充电桩特写，螺旋线缆与 Type 2 枪头整齐摆放。",
            "copy": {"headline": "家用充电桩，就该这么简洁", "primary_text": "哑光黑外壳、环形状态灯、Type 2 接口，装好就是车库里最好看的设备。"},
            "factor_values": _uses(人物=["无人"], 场景=["产品"], 汽车=["出车", "不出车"], 钩子=["一晚充满", "安全耐用"]),
            "exploratory": True,
            "positive_fragments": ["premium studio product photograph, crisp details"],
            "negative_fragments": ["distorted charger shape", "wrong logo position"],
        },
    ],
    "exclusions": [
        {"factor_a": "场景", "value_a": "产品", "factor_b": "人物", "value_b": "有人"},
    ],
    "notes": "Controlled Mock example: an EV charger plan, not produced by a model.",
}


def _test_plan_draft(request: ResolvedLlmChatRequest) -> dict[str, Any]:
    """The EV-charger example, for requests that carry the drafter's schema.

    The registry is keyed by semantic only, so another schema under this tag
    (a test, another caller) gets the schema-synthesized answer instead.
    """
    if request.schema_name != TEST_PLAN_DRAFT_SCHEMA_NAME:
        return synthesize_from_schema(request.json_schema)
    return copy.deepcopy(TEST_PLAN_DRAFT_EXAMPLE)


register_llm_mock_fixture(TEST_PLAN_DRAFT_SEMANTIC, _test_plan_draft)


# ---------------------------------------------------------------------------
# Schema synthesis
# ---------------------------------------------------------------------------


def synthesize_from_schema(schema: Mapping[str, Any], *, root: Mapping[str, Any] | None = None) -> Any:
    """The smallest deterministic instance satisfying ``schema`` (subset of
    ``app.providers.json_schema``): first enum/const value, ``minItems``
    copies of an item, every property of an object, bounds respected."""
    root = root if root is not None else schema
    if "$ref" in schema:
        node: Any = root
        for part in str(schema["$ref"])[2:].split("/"):
            node = node[part]
        return synthesize_from_schema(node, root=root)
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    if schema.get("enum"):
        return copy.deepcopy(schema["enum"][0])
    if schema.get("anyOf"):
        return synthesize_from_schema(schema["anyOf"][0], root=root)
    declared = schema.get("type", "object")
    kind = next((name for name in declared if name != "null"), declared[0]) if isinstance(declared, list) else declared
    if kind == "object":
        properties = schema.get("properties") or {}
        return {name: synthesize_from_schema(sub, root=root) for name, sub in properties.items()}
    if kind == "array":
        items = schema.get("items") or {}
        count = int(schema.get("minItems", 1) or 0)
        if "maxItems" in schema:
            count = min(count, int(schema["maxItems"]))
        return [synthesize_from_schema(items, root=root) for _ in range(count)]
    if kind == "string":
        text = "mock"
        minimum = int(schema.get("minLength", 0) or 0)
        if len(text) < minimum:
            text = text + "x" * (minimum - len(text))
        if "maxLength" in schema:
            text = text[: int(schema["maxLength"])]
        return text
    if kind in ("integer", "number"):
        value: Any = schema.get("minimum", schema.get("exclusiveMinimum", 0))
        if "exclusiveMinimum" in schema and "minimum" not in schema:
            value = value + 1
        if "maximum" in schema and value > schema["maximum"]:
            value = schema["maximum"]
        return int(value) if kind == "integer" else value
    if kind == "boolean":
        return False
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MockLlmChatAdapter:
    """``ExternalCallAdapter[ResolvedLlmChatRequest, LlmChatOutput]`` that never calls out."""

    provider: str = MOCK_PROVIDER
    capabilities: ProviderCapabilities = ProviderCapabilities(
        # Deterministic: re-asking with the same request is always safe.
        accepts_idempotency_key=True,
        supports_result_lookup_by_request_id=False,
    )

    def __init__(self, fixtures: Mapping[str, MockFixture] | None = None) -> None:
        self._fixtures = fixtures

    async def execute(
        self,
        request: ResolvedLlmChatRequest,
        *,
        credential: memoryview | None,
        idempotency_key: str,
    ) -> ProviderResult[LlmChatOutput]:
        fixtures = self._fixtures if self._fixtures is not None else _FIXTURES
        fixture = fixtures.get(request.semantic)
        if fixture is None:
            data = synthesize_from_schema(request.json_schema)
        else:
            data = fixture(request) if callable(fixture) else copy.deepcopy(dict(fixture))
        errors = json_schema.validate(data, request.json_schema)
        if errors:
            raise ProviderDispatchError(
                dispatch_certainty="not_dispatched",
                message=(
                    f"mock fixture for {request.semantic!r} does not satisfy the request schema: "
                    + "; ".join(errors[:5])
                ),
                error_code="mock_fixture_invalid",
                retryable=False,
            )
        if request.output_validator is not None:
            try:
                request.output_validator(data)
            except LlmOutputValidationError as invalid:
                raise ProviderDispatchError(
                    dispatch_certainty="not_dispatched", message=invalid.detail,
                    error_code=invalid.code, retryable=False,
                ) from invalid
        content = json.dumps(data, sort_keys=True, ensure_ascii=False)
        usage = {
            "input_tokens": request.estimated_input_tokens,
            "cached_input_tokens": 0,
            "output_tokens": min(estimate_output_tokens(content), request.max_output_tokens),
            "reasoning_tokens": 0,
        }
        response_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        output = LlmChatOutput(
            semantic=request.semantic,
            data=data,
            usage=usage,
            model_version=request.model,
            latency_ms=0,
            response_digest=response_digest,
            http_calls=0,
        )
        spec = model_spec(self.provider, request.model)
        return ProviderResult(
            output=output,
            provider_request_id=f"mock_{request.request_digest[:32]}",
            idempotency_key_accepted=True,
            reported_cost=ReportedCost(
                provider_amount=usage_cost(spec, {}),
                provider_currency="USD",
                price_rule_version=LLM_PRICE_RULE_VERSION,
                usage=usage,
                evidence={
                    "semantic": request.semantic,
                    "model_version": request.model,
                    "latency_ms": 0,
                    "http_calls": 0,
                    "request_digest": request.request_digest,
                    "response_digest": response_digest,
                    "image_sha256": [image.sha256 for image in request.images],
                    "mock_fixture": "registered" if fixture is not None else "schema_synthesized",
                    "output": data,
                },
                simulated_amount=usage_cost(spec, usage),
            ),
        )
