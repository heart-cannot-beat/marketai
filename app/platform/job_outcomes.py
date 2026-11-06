"""Explicit handler outcomes for the Worker's queue fence.

A domain handler and the queue fence around it disagree by default: the
handler knows an external call came back ambiguous, or that the work it was
admitted for no longer exists, but a Python function that returns normally
tells the fence exactly one thing -- "no exception" -- and the fence has no
choice but to call :func:`app.platform.jobs.succeed`.

That default is wrong in both directions. A coordinator result carrying
``success=False`` used to leave a ``succeeded`` Job (so its Operation
aggregated to ``succeeded`` even though nothing was produced), while an
unexceptional no-op looked identical to real work.

The vocabulary here closes that gap. Handlers signal a non-success outcome
by raising, the fence maps the signal onto the Job lifecycle, and the
Operation aggregation in :mod:`app.platform.operations` then folds the
Job states it actually finds:

============  ====================  ==========================================
outcome       Job state             meaning
============  ====================  ==========================================
(no signal)   ``succeeded``         the handler did its work
``obsolete``  ``succeeded``         nothing left to do; the admitted unit is
                                    closed and audited as ``job.obsolete``
``retry``     ``retry_wait`` then   a transient fault; the attempt budget is
              ``failed``            the queue's, and the Operation is not
                                    folded while the Job can still run
``failed``    ``failed``            a determined failure; no retry of *this*
                                    Job would be correct
``uncertain`` ``uncertain``         spec §16.8: a paid or remote call whose
                                    outcome cannot be determined. Never
                                    auto-retried, never reported as failed.
============  ====================  ==========================================
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from . import jobs
from .tenancy import TenantContext


__all__ = [
    "JobOutcome",
    "HandlerOutcomeSignal",
    "JobFailed",
    "JobRetry",
    "JobUncertain",
    "JobObsolete",
    "SettledJob",
    "settle_job_outcome",
]


class JobOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    OBSOLETE = "obsolete"
    RETRY = "retry"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class HandlerOutcomeSignal(Exception):
    """Base class for the non-success outcomes a handler can signal.

    Deliberately an exception rather than a return value: the handlers this
    serves are deep call stacks whose intermediate frames have no business
    knowing about the queue, and an outcome that has to be threaded back
    through every ``return`` is an outcome that gets dropped by the one
    frame that forgets to.
    """

    outcome: JobOutcome = JobOutcome.FAILED

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{self.outcome.value}:{code}: {detail}" if detail else f"{self.outcome.value}:{code}")


class JobFailed(HandlerOutcomeSignal):
    """A determined failure. Retrying this same Job would repeat it."""

    outcome = JobOutcome.FAILED


class JobRetry(HandlerOutcomeSignal):
    """A transient fault. The queue's own attempt budget decides."""

    outcome = JobOutcome.RETRY

    def __init__(
        self, code: str, detail: str = "", *, retry_after_seconds: float | None = None
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code, detail)


class JobUncertain(HandlerOutcomeSignal):
    """The external outcome is unknown (spec §16.8). Never auto-retried."""

    outcome = JobOutcome.UNCERTAIN


class JobObsolete(HandlerOutcomeSignal):
    """There is nothing left to do -- a replay, a cancelled parent, or work
    another delivery already folded. The Job is closed as ``succeeded``
    because the admitted unit *is* complete; the audit trail records why."""

    outcome = JobOutcome.OBSOLETE


@dataclass(frozen=True)
class SettledJob:
    """What the fence did, so callers and tests can assert on it."""

    outcome: JobOutcome
    job_state: str
    code: str = ""


def settle_job_outcome(
    context: TenantContext,
    job_id: str,
    owner: str,
    signal: HandlerOutcomeSignal | None,
) -> SettledJob:
    """Map one handler outcome onto the Job lifecycle.

    ``signal is None`` is the success path, kept here so the fence has a
    single exit and the table above is enforced in one place.
    """
    if signal is None:
        jobs.succeed(context, job_id, owner)
        return SettledJob(outcome=JobOutcome.SUCCEEDED, job_state="succeeded")

    if signal.outcome is JobOutcome.OBSOLETE:
        jobs.record_obsolete(context, job_id, code=signal.code, detail=signal.detail)
        jobs.succeed(context, job_id, owner)
        return SettledJob(
            outcome=JobOutcome.OBSOLETE, job_state="succeeded", code=signal.code
        )

    if signal.outcome is JobOutcome.UNCERTAIN:
        jobs.mark_uncertain(
            context, job_id, owner, code=signal.code, detail=signal.detail
        )
        return SettledJob(
            outcome=JobOutcome.UNCERTAIN, job_state="uncertain", code=signal.code
        )

    retryable = signal.outcome is JobOutcome.RETRY
    state = jobs.fail(
        context,
        job_id,
        owner,
        code=signal.code,
        detail=signal.detail,
        retryable=retryable,
        retry_after_seconds=(
            signal.retry_after_seconds if isinstance(signal, JobRetry) else None
        ),
    )
    return SettledJob(outcome=signal.outcome, job_state=state, code=signal.code)
