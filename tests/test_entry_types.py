"""Registration evidence for the six #20 entry types (templates, prompt policy, datasources)."""
import unittest

from app.library.contracts import (
    ENTRY_TYPES, OWNER_ONLY_PUBLISH, ORIGIN_COMMANDS,
    ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA,
    ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
    ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY,
    COMMAND_APPLY,
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

    def test_command_apply(self):
        self.assertIn(COMMAND_APPLY, ORIGIN_COMMANDS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
