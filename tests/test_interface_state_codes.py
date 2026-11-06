"""The code sets the interface words (#286) match what the services write.

The web guard (``web/__tests__/locales/state-codes.spec.ts``) requires copy
for every value of these contract enums; these tests keep each enum equal to
the values the backend can actually store, so a new stage or step cannot reach
the page without first widening the enum (and with it, the copy check).
"""
import re
from pathlib import Path
from typing import get_args

from app.adapters.persistence.models import LocalizationBatchRecord
from app.launch.api import LaunchStepType
from app.localization.response_models import PreparationStage

ROOT = Path(__file__).resolve().parents[1]


def test_preparation_stage_matches_the_batch_check_constraint():
    (check,) = [c for c in LocalizationBatchRecord.__table__.constraints
                if str(getattr(c, "name", "")).endswith("ck_lb_preparation_stage")]
    stored = set(re.findall(r"'([a-z_]+)'", str(check.sqltext)))
    assert stored == set(get_args(PreparationStage))


def test_launch_step_type_matches_the_steps_the_saga_records():
    written = set()
    for source in (ROOT / "app" / "launch").glob("*.py"):
        written |= set(re.findall(r'step_type="([a-z_]+)"', source.read_text()))
    assert written == set(get_args(LaunchStepType))
