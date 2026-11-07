"""Registration evidence for the six #20 entry types (templates, prompt policy, datasources)."""
import unittest

from app.library.contracts import (
    ENTRY_TYPES, OWNER_ONLY_PUBLISH, ORIGIN_COMMANDS,
    ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA,
    ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
    ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY,
    COMMAND_APPLY, ROLE_MEMBER, ROLE_OWNER, can_publish,
)


class TestEntryTypeRegistration(unittest.TestCase):
    def test_new_types_registered(self):
        for t in (
            ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA,
            ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
            ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY,
        ):
            self.assertIn(t, ENTRY_TYPES)

    def test_owner_only_publish(self):
        for t in (ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
                  ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY):
            self.assertIn(t, OWNER_ONLY_PUBLISH)
        for t in (ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA):
            self.assertNotIn(t, OWNER_ONLY_PUBLISH)

    def test_can_publish_holds_back_only_a_member_on_owner_only_types(self):
        # One rule for the entry's publish eligibility and the publish command (#24).
        for t in sorted(ENTRY_TYPES):
            with self.subTest(entry_type=t):
                self.assertTrue(can_publish(t, ROLE_OWNER))
                self.assertTrue(can_publish(t, "system"))
                self.assertEqual(can_publish(t, ROLE_MEMBER), t not in OWNER_ONLY_PUBLISH)

    def test_command_apply(self):
        self.assertIn(COMMAND_APPLY, ORIGIN_COMMANDS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
