"""Prompt policy: platform baseline, workspace policy, deterministic composition.

契约 §10：Prompt 由三层按版本化顺序组装 —— PlatformPromptBaselineVersion（不可关闭
的安全/肖像/产品保真/Provider 限制）、WorkspacePromptPolicyVersion（Owner 发布的品
牌语气、允许模板、可选条款、锁定短语）、Run 输入（已发布产品、must-match、人物、
维度、意图、画幅、负面约束）。规则只允许注册 Token，执行单遍替换；未知 Token、缺
少必填 Token、递归展开或无法通过 Schema 的内容阻止发布（递归检测在
``app.library.contracts._validate_prompt_policy``，publish-time 静态校验）。用户删
除受保护短语时，系统在草稿保存时恢复完整受保护条款并给出告警。

``publish_baseline``/``withdraw_baseline`` 操作 ``prompt_baselines`` 表 —— 该表没
有 workspace_id（平台级、非租户资源，见 Task 1 的
``app.adapters.persistence.models.PromptBaselineRecord``），因此使用
``platform_session()`` 而不是 ``tenant_session()``。记录本身只保存单一
``published_by``/``withdrawn_by`` 字符串（不是创意库条目那套
initiator_id/initiator_role/executor 三元组审计），这是 Task 1 既定的表结构。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select

from app.adapters.persistence.database import platform_session
from app.adapters.persistence.models import PromptBaselineRecord, new_id, utcnow
from app.library.contracts import Finding, SEVERITY_WARNING, canonical_hash

# 注册 Token 语法：{{name}}，name 必须是合法标识符。
TOKEN_PATTERN = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


@dataclass(frozen=True)
class ComposedPrompt:
    """一次确定性组装的结果，进入 GenerationRecipeSnapshot（契约 §10）。"""

    final_text: str
    layer_hashes: dict[str, str]
    token_resolution_log: list[dict]
    composition_hash: str


# ---------------------------------------------------------------------------
# Platform baseline lifecycle (publish / withdraw / read effective).
# ---------------------------------------------------------------------------


def publish_baseline(content: dict, published_by: str) -> dict:
    """发布新的 PlatformPromptBaselineVersion（仅平台管理员）。

    追加式：若已有 effective 版本，先转为 superseded，再插入下一个版本号。版本号
    单调递增覆盖整个历史（``uq_prompt_baseline_version_no`` 是全表唯一约束，不是
    "只在 effective 内唯一"），所以下一个版本号必须从历史最大值算起 —— 不能只看
    当前 effective 行：一次 withdraw 之后可能没有任何 effective 行，但历史版本号
    仍然存在，此时如果把 next_no 重置回 1 会撞上旧版本号的唯一约束。
    """
    with platform_session() as session:
        current = session.scalars(
            select(PromptBaselineRecord).where(PromptBaselineRecord.state == "effective")
        ).one_or_none()
        if current is not None:
            current.state = "superseded"
            current.superseded_at = utcnow()
        max_version_no = session.scalar(select(func.max(PromptBaselineRecord.version_no)))
        next_no = (max_version_no or 0) + 1
        record = PromptBaselineRecord(
            id=new_id("pbl"),
            version_no=next_no,
            content_json=content,
            content_hash=canonical_hash(content),
            state="effective",
            published_by=published_by,
        )
        session.add(record)
        session.flush()
        return {
            "id": record.id,
            "version_no": record.version_no,
            "content_hash": record.content_hash,
            "state": record.state,
        }


def withdraw_baseline(baseline_id: str, reason: str, withdrawn_by: str) -> dict:
    """紧急撤回一个 effective 基线（契约 §10：尚未发出 Provider 调用的任务须暂停重新预览）。"""
    with platform_session() as session:
        record = session.get(PromptBaselineRecord, baseline_id)
        if record is None:
            raise ValueError("Baseline not found")
        if record.state != "effective":
            raise ValueError(f"Cannot withdraw: state is {record.state}")
        record.state = "withdrawn"
        record.withdrawn_at = utcnow()
        record.withdraw_reason = reason
        record.withdrawn_by = withdrawn_by
        # TODO(#21): 一旦 outbox/domain event 机制落地，这里要发出下游事件，
        # 暂停仍引用该基线且尚未发出 Provider 调用的任务，并要求重新预览。
        session.flush()
        return {
            "id": record.id,
            "state": record.state,
            "withdraw_reason": reason,
            "withdrawn_by": withdrawn_by,
        }


def get_effective_baseline() -> dict | None:
    """返回当前 effective 基线内容；不存在则返回 None。"""
    with platform_session() as session:
        record = session.scalars(
            select(PromptBaselineRecord).where(PromptBaselineRecord.state == "effective")
        ).one_or_none()
        if record is None:
            return None
        return dict(record.content_json)


# ---------------------------------------------------------------------------
# Locked-phrase normalization (save-time, §10).
# ---------------------------------------------------------------------------


def normalize_draft_phrases(
    content: dict,
    effective_baseline: dict,
) -> tuple[dict, list[Finding]]:
    """草稿保存时的规范化：恢复被移除的受保护短语（契约 §10）。

    用户删除受保护短语时，系统恢复完整受保护条款、给出告警，要求重新确认后
    才能发布新的 WorkspacePromptPolicyVersion。返回 (normalized_content, findings)；
    调用方负责递增草稿 revision 并要求重新确认。
    """
    findings: list[Finding] = []
    baseline_clauses = effective_baseline.get("clauses") or []
    protected = [c for c in baseline_clauses if c.get("locked")]
    existing_ids = {
        p.get("clause_id") for p in (content.get("locked_phrases") or [])
    }
    restored = list(content.get("locked_phrases") or [])
    for clause in protected:
        if clause["clause_id"] not in existing_ids:
            restored.append(
                {
                    "phrase": clause["text"],
                    "clause_id": clause["clause_id"],
                    "protected": True,
                }
            )
            findings.append(
                Finding(
                    "protected_phrase_restored",
                    f"受保护短语 {clause['clause_id']!r} 已被移除并自动恢复，发布前需重新确认。",
                    "locked_phrases",
                    SEVERITY_WARNING,
                )
            )
    normalized = dict(content)
    normalized["locked_phrases"] = restored
    return normalized, findings


# ---------------------------------------------------------------------------
# Deterministic composition (§10).
# ---------------------------------------------------------------------------


def _merge_registered_tokens(baseline_content: dict, policy_content: dict) -> dict[str, dict]:
    """注册 Token 全集：Workspace 策略 Token 优先，平台基线 Token 补齐未覆盖的名字。"""
    registered: dict[str, dict] = {
        t["name"]: t for t in policy_content.get("registered_tokens") or [] if t.get("name")
    }
    for pt in baseline_content.get("platform_tokens") or []:
        name = pt.get("name")
        if name and name not in registered:
            registered[name] = pt
    return registered


def _resolve_registered_tokens(
    registered: dict[str, dict], run_inputs: dict
) -> tuple[dict[str, str], list[dict]]:
    """预先解析每个注册 Token 的值（run_input 优先于 default）。

    在做任何文本替换之前把全部值算好，是保证单遍替换、不递归展开的关键：
    后面的替换只查这个已经算好的字典，不会再对结果重新求值。
    """
    resolved: dict[str, str] = {}
    log: list[dict] = []
    for name in sorted(registered):
        token_def = registered[name]
        if name in run_inputs:
            value = str(run_inputs[name])
            source = "run_input"
        elif "default" in token_def:
            value = str(token_def["default"])
            source = "default"
        elif token_def.get("required"):
            raise ValueError(f"缺少必填 token: {name}")
        else:
            value = ""
            source = "empty"
        resolved[name] = value
        log.append({"token": name, "resolved_to": value, "source": source})
    return resolved, log


def compose_prompt(
    baseline_content: dict,
    policy_content: dict,
    run_inputs: dict,
) -> ComposedPrompt:
    """确定性单遍 Prompt 组装（契约 §10）。

    三层顺序：平台基线条款 → Workspace 策略（锁定短语/可选条款/品牌语气）→
    Run 输入。基线/策略层的文本可以包含 ``{{token}}`` 占位符，用预先解析好
    的注册 Token 值做一次性替换；未知 Token 或缺少必填 Token 都会阻断。Run
    输入层（包括已解析的 Token 取值本身）以字面量写入，不再参与替换 ——
    这正是"单遍、不递归展开"的含义：即便某个 Token 的取值本身包含
    ``{{...}}`` 语法（例如运行期真实产品名恰好带这个模式），它也只会原样
    出现在最终文本里，不会被再次当成占位符展开。
    """
    registered = _merge_registered_tokens(baseline_content, policy_content)
    resolved, log = _resolve_registered_tokens(registered, run_inputs)

    def substitute(text: str) -> str:
        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in resolved:
                raise ValueError(f"未知 token: {name}")
            return resolved[name]

        return TOKEN_PATTERN.sub(_replace, text)

    # Layer 1: 平台基线条款，按 clause_id 排序保证确定性。
    baseline_clauses = sorted(
        baseline_content.get("clauses") or [],
        key=lambda c: c.get("clause_id", ""),
    )
    baseline_text = "\n".join(c["text"] for c in baseline_clauses if c.get("text"))

    # Layer 2: Workspace 策略 —— 锁定短语、可选条款、品牌语气。
    policy_phrases = policy_content.get("locked_phrases") or []
    phrase_text = "\n".join(p["phrase"] for p in policy_phrases if p.get("phrase"))
    optional_clauses = policy_content.get("optional_clauses") or []
    optional_text = "\n".join(
        c["text"] for c in optional_clauses if isinstance(c, dict) and c.get("text")
    )
    tone = policy_content.get("brand_tone") or {}
    tone_line = f"Tone: {tone['style']}" if tone.get("style") else ""
    policy_text = "\n".join(p for p in (phrase_text, optional_text, tone_line) if p)

    # 只对模板层（基线 + 策略）做单遍替换；它们可能含 {{token}} 占位符。
    template_text = "\n\n".join(t for t in (baseline_text, policy_text) if t)
    substituted_template = substitute(template_text) if template_text else ""

    # Layer 3: Run 输入 —— 字面量，不再替换。
    run_parts: list[str] = []
    if run_inputs.get("product_description"):
        run_parts.append(f"Product: {run_inputs['product_description']}")
    for mm in run_inputs.get("must_match") or []:
        run_parts.append(f"Must match: {mm}")
    for frag in run_inputs.get("person_fragments") or []:
        run_parts.append(str(frag))
    dims = run_inputs.get("dimension_values") or {}
    for dim_name in sorted(dims):
        run_parts.append(f"{dim_name}: {dims[dim_name]}")
    if run_inputs.get("intent"):
        run_parts.append(f"Intent: {run_inputs['intent']}")
    if run_inputs.get("aspect_ratio"):
        run_parts.append(f"Aspect ratio: {run_inputs['aspect_ratio']}")
    for neg in run_inputs.get("negatives") or []:
        run_parts.append(f"Avoid: {neg}")
    # 已解析的注册 Token 取值本身也属于 Run 输入层：按名字排序，字面量写入。
    for name in sorted(resolved):
        run_parts.append(f"{name}: {resolved[name]}")
    run_text = "\n".join(run_parts)

    final_text = "\n\n".join(t for t in (substituted_template, run_text) if t)

    layer_hashes = {
        "baseline": canonical_hash(baseline_text),
        "policy": canonical_hash(policy_text),
        "run_inputs": canonical_hash(run_parts),
    }
    composition_hash = canonical_hash(final_text)

    return ComposedPrompt(
        final_text=final_text,
        layer_hashes=layer_hashes,
        token_resolution_log=log,
        composition_hash=composition_hash,
    )
