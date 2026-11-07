"""Register probe Job types for tests that need a type no Worker dispatches.

``enqueue_in_session`` refuses a type ``app.platform.job_policy`` does not
list (#22). A test that only exercises the queue itself -- lanes, epochs,
tenant boundaries -- registers its made-up type for the duration instead.
"""
from __future__ import annotations

from unittest.mock import patch

from app.platform.job_policy import JOB_POLICIES, CrashPolicy, JobPolicy, SideEffect


def probe_job_types(
    *job_types: str,
    side_effect: SideEffect = SideEffect.NONE,
    crash: CrashPolicy = CrashPolicy.REQUEUE,
):
    """A ``patch.dict`` registering ``job_types`` with one policy."""
    policy = JobPolicy(side_effect, crash, "test probe")
    return patch.dict(JOB_POLICIES, {job_type: policy for job_type in job_types})
