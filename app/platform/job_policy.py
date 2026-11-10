"""What a Worker crash means for each durable Job type (#22).

A Job whose Worker dies after ``mark_running`` is recovered when its lease
expires -- by ``jobs.recover_expired_jobs`` on SQLite and inside
``marketai_claim_next_job_v2`` on PostgreSQL. Both read one column,
``jobs.safe_to_retry_after_crash``: set, the Job goes back to ``queued``
(``lease_expired``); unset, it is left ``uncertain``
(``paid_call_outcome_unknown``) so a paid or external call is never sent
twice behind the user's back.

That column used to default to ``False`` at every enqueue point, so a Job
with no external call at all came back from a crash reported as a paid call
of unknown outcome, and its legacy Operation stayed ``running``. This module
is the one place the answer is written down: ``enqueue_in_session`` derives
the column from it and refuses a Job type it does not list.

``crash=REQUEUE`` is a promise about the handler, not just about the call
it makes: a rerun checks for a terminal state first and closes only the
Job, and the final write happens once, under the resource's or the
Operation's row lock.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SideEffect(str, Enum):
    NONE = "none"
    """Writes only this database and the platform BlobStore, or reads elsewhere."""

    PAID = "paid"
    """May make a billable provider call."""

    EXTERNAL_WRITE = "external_write"
    """May change state on a third-party system (Meta)."""


class CrashPolicy(str, Enum):
    REQUEUE = "requeue"
    """An expired lease puts the Job back on the queue."""

    UNCERTAIN = "uncertain"
    """An expired lease after ``mark_running`` leaves the Job ``uncertain``."""


@dataclass(frozen=True)
class JobPolicy:
    side_effect: SideEffect
    crash: CrashPolicy
    note: str = ""
    requeue_when_fenced: bool = False
    """The enqueuer may mark one Job of this ``UNCERTAIN`` type requeue-safe
    when it knows that run makes no unfenced paid call (Controlled Mock, or
    a handler that checks a committed fence first). Nothing else may."""


class UnregisteredJobTypeError(ValueError):
    def __init__(self, job_type: str) -> None:
        super().__init__(
            f"job type {job_type!r} has no crash policy in app.platform.job_policy"
        )
        self.job_type = job_type


class JobPolicyMismatchError(ValueError):
    pass


_NONE_REQUEUE = (SideEffect.NONE, CrashPolicy.REQUEUE)
_PAID_REQUEUE = (SideEffect.PAID, CrashPolicy.REQUEUE)
_PAID_UNCERTAIN = (SideEffect.PAID, CrashPolicy.UNCERTAIN)


def _policy(kind: tuple[SideEffect, CrashPolicy], note: str = "", **kwargs) -> JobPolicy:
    side_effect, crash = kind
    return JobPolicy(side_effect=side_effect, crash=crash, note=note, **kwargs)


#: Every Job type a Worker dispatches. ``tests/test_job_policy.py`` fails when
#: ``WorkerRuntime`` or ``ScaleWorkerRuntime`` learns a type this does not list.
JOB_POLICIES: dict[str, JobPolicy] = {
    # -- No external call. A rerun converges on the committed outcome. ------
    "library.reference_gate": _policy(
        _NONE_REQUEUE, "内容安全 Adapter 尚未接入；只读写 BlobStore。最终写入在参考对象行锁下（#22）"),
    "generation.export_package": _policy(
        _NONE_REQUEUE, "只打 ZIP 写 BlobStore；最终写入在 ExportPackage 行锁下（#22）"),
    "scale_export": _policy(
        _NONE_REQUEUE, "结果引用仍是占位；最终写入在 ScaleExport 行锁下（#22）。生产 Worker 自 #55 起分发"),
    "localization.run_deterministic_qc": _policy(
        _NONE_REQUEUE, "本地检查，单事务，在 Item 行锁下（#22）"),
    "localization.finalize_item": _policy(
        _NONE_REQUEUE, "本地发布，单事务，在 Item 行锁下（#22）"),
    "localization.fold_batch": _policy(
        _NONE_REQUEUE, "本地折叠，单事务，在 Batch 行锁下（#22）"),
    "library.connection_check": _policy(_NONE_REQUEUE, "本地检查；Operation 与证据同一事务"),
    "library.manual_fact_import": _policy(_NONE_REQUEUE, "草案与 Operation 在 Operation 行锁下同一事务（#14）"),
    "verify_upload_intent": _policy(_NONE_REQUEUE, "只读校验暂存对象"),
    "cleanup_staging_upload": _policy(_NONE_REQUEUE, "BlobStore.delete 对已删除对象视为成功"),
    "meta_read_ingestion": _policy(_NONE_REQUEUE, "只读 Meta"),
    "meta_schedule_tick": _policy(_NONE_REQUEUE, "由 PostgreSQL 调度函数直接插入，SQL 已写 true"),
    "meta_observation_tick": _policy(
        _NONE_REQUEUE,
        "PG 上由 marketai_dispatch_meta_observation_ticks_v1 插入，SQL 已写 true；"
        "在计划行锁下复核到期，推进 next_fire_at 与建 run 同一事务（#107）"),
    "meta_observation_sync": _policy(
        _NONE_REQUEUE,
        "只读沙箱 insights 和投放配置；每个查询实例一个 run，按页断点续跑，"
        "终页、完成、发布、attestation 同一事务（#107、#108）"),
    "launch_recovery": _policy(_NONE_REQUEUE, "只读外部证据来解析不确定的写入，不重放"),
    "marketing.resolve_call": _policy(_NONE_REQUEUE, "核对已有调用记录，不重发"),
    "marketing.export": _policy(_NONE_REQUEUE, "只写 BlobStore"),
    "scale_evaluate": _policy(
        _NONE_REQUEUE,
        "Phase A/B/C 检查点可续跑（tests/test_scale_recovery.py）；收集器写 complete manifest 时"
        "同一事务建 run 并入队；Episode 被前一个 run 占着时可重试，不终结 run（#110）"),
    "scale_advice_expire": _policy(
        _NONE_REQUEUE, "在线程行锁下复查，非 active 即不写；Phase C 按截止时间延迟入队（#111）"),
    "scale_collect_sources": _policy(
        _NONE_REQUEUE,
        "只读已存的观察页，不调用适配器；每个 Episode 在行锁下单事务写 manifest，"
        "按收集键找回已写的，重放不分配新 revision（#109）；"
        "之后兜底扫描该连接上过了截止时间的 active Advice thread，在线程行锁下复查，重放不重复过期（#111）"),
    # -- Paid, but the handler fences the call through its own durable record.
    "marketing.plan": _policy(_PAID_REQUEUE, "调用记录挡住重复付费"),
    "marketing.ai_qc": _policy(_PAID_REQUEUE, "调用记录挡住重复付费"),
    "marketing.image_edit": _policy(_PAID_REQUEUE, "编辑记录挡住重复付费"),
    "competitor.resolve": _policy(_PAID_REQUEUE, "经 ExternalCallCoordinator，按调用键回放证据"),
    "competitor_scan.run": _policy(_PAID_REQUEUE, "每个动作经 ExternalCallCoordinator，按调用键回放证据"),
    # -- External write fenced by Saga markers.
    "launch_canary": JobPolicy(
        SideEffect.EXTERNAL_WRITE, CrashPolicy.REQUEUE, "Saga 每步带 marker，重跑先读回再决定"),
    # -- Paid or external write whose outcome a crash leaves unknown. --------
    "generate_asset": _policy(
        _PAID_UNCERTAIN, "Mock provider 由入队点标为可回队", requeue_when_fenced=True),
    "generation.run_item": _policy(
        _PAID_UNCERTAIN, "selection 配方的 handler 先查已提交的 RunItem/Attempt，由入队点标为可回队",
        requeue_when_fenced=True),
    "generation.qc_asset_version": _policy(_PAID_UNCERTAIN, "VLM QC 经 llm_chat"),
    "products.intake": _policy(_PAID_UNCERTAIN, "网页抓取加 llm_chat；Mock 模式可能被误标"),
    "test_plans.draft": _policy(_PAID_UNCERTAIN, "BYOK 时走 llm_chat；Mock 模式可能被误标"),
    "external_creatives.process": _policy(_PAID_UNCERTAIN, "llm_chat；Mock 模式可能被误标"),
    "localization.analyze_source": _policy(_PAID_UNCERTAIN, "经 coordinator；崩溃策略待单独一片"),
    "localization.translate_copy": _policy(_PAID_UNCERTAIN, "经 coordinator；崩溃策略待单独一片"),
    "localization.edit_attempt": _policy(_PAID_UNCERTAIN, "经 coordinator；崩溃策略待单独一片"),
    "localization.ocr_check": _policy(
        _PAID_UNCERTAIN, "按付费步骤准入；目前是本地占位实现，崩溃策略待单独一片"),
    "localization.visual_qc": _policy(
        _PAID_UNCERTAIN, "按付费步骤准入；目前是本地占位实现，崩溃策略待单独一片"),
    "launch_provisioning": JobPolicy(
        SideEffect.EXTERNAL_WRITE, CrashPolicy.UNCERTAIN, "Meta 写入；恢复时 Operation 一并转 uncertain"),
}


def policy_for(job_type: str) -> JobPolicy:
    try:
        return JOB_POLICIES[job_type]
    except KeyError:
        raise UnregisteredJobTypeError(job_type) from None


def resolve_safe_to_retry_after_crash(job_type: str, requested: bool | None) -> bool:
    """The ``safe_to_retry_after_crash`` value a new Job of ``job_type`` gets.

    ``requested`` is what the enqueuer passed, ``None`` when it left the
    choice to this registry. A requeue type cannot be enqueued as
    ``uncertain``, and an ``uncertain`` type can be marked requeue-safe only
    where its policy says the enqueuer knows enough to do so.
    """
    policy = policy_for(job_type)
    registered = policy.crash is CrashPolicy.REQUEUE
    if requested is None or requested == registered:
        return registered
    if requested and policy.requeue_when_fenced:
        return True
    raise JobPolicyMismatchError(
        f"job type {job_type!r} is registered crash={policy.crash.value}; "
        f"the enqueuer asked for safe_to_retry_after_crash={requested}"
    )
