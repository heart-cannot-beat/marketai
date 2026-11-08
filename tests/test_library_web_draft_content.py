"""The web draft fixtures are content the server's validator accepts (#21).

`web/__tests__/fixtures/library-draft-content.json` holds one draft content
per Creative Library entry type, written with the server's field names only.
The web codec tests round-trip every one of them through the draft editor's
codec; this test keeps them honest, so a renamed or newly required field on
the server fails here instead of leaving the web tests on stale names.

It checks content shape only (`validate_content`). Whether the ids they
reference exist is the publish dependency check, which the service tests
cover (`tests/test_library_core.py`).
"""
import json
import unittest
from datetime import date
from pathlib import Path

from app.library.contracts import ENTRY_TYPES, validate_content

FIXTURE = Path(__file__).resolve().parents[1] / "web" / "__tests__" / "fixtures" / "library-draft-content.json"
TODAY = date(2026, 11, 8)


class WebDraftContentFixtureTests(unittest.TestCase):
    def test_every_fixture_passes_the_content_validator(self) -> None:
        contents = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertTrue(contents)
        for entry_type, content in contents.items():
            with self.subTest(entry_type=entry_type):
                self.assertIn(entry_type, ENTRY_TYPES)
                report = validate_content(entry_type, content, today=TODAY).as_dict()
                self.assertEqual(report["blocking"], [])
                self.assertEqual(report["warnings"], [])


if __name__ == "__main__":
    unittest.main()
