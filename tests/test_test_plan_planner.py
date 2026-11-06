"""Deterministic test-plan planner (Issue #163, contract §5.3).

Pure functions, no database: the three scales on a known factor set, the
untested-combination summary, determinism, constraint pruning and the
refusals that keep a plan enumerable.
"""
from __future__ import annotations

import unittest

from app.generation.planner import (
    FULL_TEST_CAP,
    SMALL_TEST_CAP,
    PlanDirectionSpace,
    PlannerError,
    enumerate_plan_space,
    plan_all_stages,
    plan_test_stage,
)


def four_directions() -> list[PlanDirectionSpace]:
    """4 directions x (3 scenes x 2 angles x 2 people) = 48 combinations."""
    scenes = ["scene-1", "scene-2", "scene-3", "scene-4", "scene-5", "scene-6"]
    return [
        PlanDirectionSpace(
            key=f"dir-{index + 1}",
            factor_values={
                "scene": scenes[index:index + 3],
                "angle": ["angle-1", "angle-2"],
                "people": ["people-1", "people-2"],
            },
        )
        for index in range(4)
    ]


def three_small_directions() -> list[PlanDirectionSpace]:
    """3 directions x (2 scenes x 2 angles x 2 people) = 24 combinations."""
    return [
        PlanDirectionSpace(
            key=f"dir-{index + 1}",
            factor_values={
                "scene": [f"scene-{index + 1}", f"scene-{index + 2}"],
                "angle": ["angle-1", "angle-2"],
                "people": ["people-1", "people-2"],
            },
        )
        for index in range(3)
    ]


class ScaleCountTests(unittest.TestCase):
    def test_three_scales_on_a_48_combination_plan(self):
        stages = plan_all_stages(four_directions())

        self.assertEqual(len(stages["sample"].space), 48)
        self.assertEqual(len(stages["sample"].selected), 4)
        self.assertEqual(len(stages["small"].selected), SMALL_TEST_CAP)
        self.assertEqual(len(stages["full"].selected), FULL_TEST_CAP)

        self.assertEqual(stages["sample"].untested_summary()["count"], 44)
        self.assertEqual(stages["small"].untested_summary()["count"], 36)
        self.assertEqual(stages["full"].untested_summary()["count"], 16)

    def test_full_is_the_whole_space_up_to_32(self):
        stages = plan_all_stages(three_small_directions())

        self.assertEqual(len(stages["full"].selected), 24)
        self.assertEqual(stages["full"].untested, [])
        summary = stages["full"].untested_summary()
        self.assertEqual(summary["count"], 0)
        self.assertIn("都会被测到", summary["summary"])
        self.assertEqual(len(stages["small"].selected), 12)
        self.assertEqual(len(stages["sample"].selected), 3)

    def test_sample_takes_exactly_one_combination_per_direction(self):
        sample = plan_test_stage(four_directions(), "sample")

        self.assertEqual([combo.direction for combo in sample.selected], ["dir-1", "dir-2", "dir-3", "dir-4"])
        # Later directions prefer values earlier samples have not shown yet.
        angles = [dict(combo.cells)["angle"] for combo in sample.selected]
        people = [dict(combo.cells)["people"] for combo in sample.selected]
        self.assertEqual(set(angles), {"angle-1", "angle-2"})
        self.assertEqual(set(people), {"people-1", "people-2"})

    def test_small_covers_every_single_value(self):
        small = plan_test_stage(four_directions(), "small")

        covered = {cell for combo in small.selected for cell in combo.dimensions().items()}
        every = {cell for combo in small.space for cell in combo.dimensions().items()}
        self.assertEqual(covered, every)
        self.assertEqual(len({combo.signature for combo in small.selected}), SMALL_TEST_CAP)

    def test_full_beyond_32_is_coverage_sampled(self):
        full = plan_test_stage(four_directions(), "full")

        covered = {cell for combo in full.selected for cell in combo.dimensions().items()}
        every = {cell for combo in full.space for cell in combo.dimensions().items()}
        self.assertEqual(covered, every)
        self.assertTrue({combo.signature for combo in full.selected} <= {combo.signature for combo in full.space})


class SummaryAndDeterminismTests(unittest.TestCase):
    def test_untested_summary_breaks_down_by_direction(self):
        titles = {"dir-1": "痛点", "dir-2": "场景", "dir-3": "质感", "dir-4": "口碑"}
        summary = plan_test_stage(four_directions(), "full").untested_summary(titles)

        self.assertEqual(summary["space_count"], 48)
        self.assertEqual(sum(item["untested_count"] for item in summary["by_direction"]), 16)
        self.assertTrue(all(item["title"] == titles[item["direction_id"]] for item in summary["by_direction"]))
        self.assertIn("未测到 16 个", summary["summary"])
        self.assertLessEqual(len(summary["examples"]), 5)
        self.assertIn("direction", summary["examples"][0])

    def test_the_same_plan_always_selects_the_same_combinations(self):
        for scale in ("sample", "small", "full"):
            first = [combo.signature for combo in plan_test_stage(four_directions(), scale).selected]
            second = [combo.signature for combo in plan_test_stage(four_directions(), scale).selected]
            self.assertEqual(first, second, scale)

    def test_signature_is_plan_local_and_sorted(self):
        sample = plan_test_stage(three_small_directions(), "sample")
        self.assertEqual(
            sample.selected[0].signature,
            "direction=dir-1|angle=angle-1|people=people-1|scene=scene-1",
        )


class ConstraintTests(unittest.TestCase):
    def test_constraints_prune_before_sampling_and_are_reported(self):
        constraints = [{
            "factor_a": "angle", "value_a": "angle-2",
            "factor_b": "people", "value_b": "people-2",
        }]
        stages = plan_all_stages(four_directions(), constraints)

        # 48 - 4 directions x 3 scenes x (angle-2 & people-2) = 36.
        self.assertEqual(len(stages["full"].space), 36)
        self.assertEqual(len(stages["full"].constraint_excluded), 12)
        self.assertEqual(len(stages["full"].selected), FULL_TEST_CAP)
        self.assertTrue(all(
            not (dict(combo.cells)["angle"] == "angle-2" and dict(combo.cells)["people"] == "people-2")
            for stage in stages.values() for combo in stage.selected
        ))
        self.assertIn("因约束被排除", stages["full"].untested_summary()["summary"])

    def test_directions_must_declare_the_same_factors(self):
        directions = three_small_directions()
        directions[1] = PlanDirectionSpace("dir-2", {"scene": ["scene-1"], "angle": ["angle-1"]})
        with self.assertRaises(PlannerError) as caught:
            enumerate_plan_space(directions)
        self.assertEqual(caught.exception.code, "direction_factor_mismatch")

    def test_a_direction_too_large_is_refused(self):
        values = [f"v-{i}" for i in range(9)]
        direction = PlanDirectionSpace("dir-1", {"a": values, "b": values})
        with self.assertRaises(PlannerError) as caught:
            enumerate_plan_space([direction])
        self.assertEqual(caught.exception.code, "direction_space_too_large")

    def test_unknown_scale_is_refused(self):
        with self.assertRaises(PlannerError) as caught:
            plan_test_stage(four_directions(), "huge")
        self.assertEqual(caught.exception.code, "plan_scale_invalid")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
