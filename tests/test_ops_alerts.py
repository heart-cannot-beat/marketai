"""Tests for structured operational alerts — redaction and emission."""
from __future__ import annotations

import json
import unittest

from app.platform.ops_alerts import (
    AlertCategory,
    Severity,
    emit,
    redact,
    redact_dict,
)

_JWT = "ey" + "JhbGciOiJFUzI1NiJ9.payload.sig"
_META_TOKEN = "EAA" + "BwzLixnjYBO0ZD"
_META_DETAIL_TOKEN = "EAA" + "Bsomething"
_META_LEAK_TOKEN = "EAA" + "BleakedToken"
_API_KEY = "s" + "k-1234567890abcdef"


class TestRedaction(unittest.TestCase):
    def test_redacts_bearer_token(self):
        text = f"Authorization: Bearer {_JWT}"
        result = redact(text)
        self.assertNotIn("eyJ", result)
        self.assertIn("[REDACTED]", result)

    def test_redacts_meta_access_token(self):
        text = f"token={_META_TOKEN}"
        result = redact(text)
        self.assertNotIn(_META_TOKEN, result)

    def test_redacts_secret_keywords(self):
        text = f"api_key={_API_KEY}"
        result = redact(text)
        self.assertIn("[REDACTED]", result)

    def test_truncates_long_strings(self):
        text = "x" * 1000
        result = redact(text)
        self.assertLessEqual(len(result), 520)
        self.assertTrue(result.endswith("...[truncated]"))

    def test_preserves_safe_strings(self):
        text = "job_abc123 completed in workspace ws_test"
        self.assertEqual(redact(text), text)

    def test_empty_string(self):
        self.assertEqual(redact(""), "")


class TestRedactDict(unittest.TestCase):
    def test_redacts_secret_keys(self):
        data = {"access_token": "EAABreal", "job_id": "job_123"}
        result = redact_dict(data)
        self.assertEqual(result["access_token"], "[REDACTED]")
        self.assertEqual(result["job_id"], "job_123")

    def test_redacts_nested_dicts(self):
        data = {"outer": {"api_key": "secret123", "status": "ok"}}
        result = redact_dict(data)
        self.assertEqual(result["outer"]["api_key"], "[REDACTED]")
        self.assertEqual(result["outer"]["status"], "ok")

    def test_redacts_secret_in_string_values(self):
        data = {"detail": f"failed with token={_META_DETAIL_TOKEN}"}
        result = redact_dict(data)
        self.assertNotIn(_META_DETAIL_TOKEN, result["detail"])


class TestEmit(unittest.TestCase):
    def test_emits_structured_payload(self):
        payload = emit(
            Severity.WARNING,
            AlertCategory.UNCERTAIN_STATE,
            "Job outcome uncertain",
            workspace_id="ws_test",
            job_id="job_abc",
            operation_id="op_xyz",
        )
        self.assertTrue(payload["alert"])
        self.assertEqual(payload["severity"], "warning")
        self.assertEqual(payload["category"], "uncertain_state")
        self.assertEqual(payload["workspace_id"], "ws_test")
        self.assertEqual(payload["job_id"], "job_abc")
        self.assertIn("ts", payload)

    def test_emit_redacts_details(self):
        payload = emit(
            Severity.CRITICAL,
            AlertCategory.RECONCILIATION,
            "Recovery sweep completed",
            details={"credential_secret": "mysecret", "count": 3},
        )
        self.assertEqual(payload["details"]["credential_secret"], "[REDACTED]")
        self.assertEqual(payload["details"]["count"], 3)

    def test_emit_omits_empty_identifiers(self):
        payload = emit(Severity.INFO, AlertCategory.SHUTDOWN, "Worker stopping")
        self.assertNotIn("workspace_id", payload)
        self.assertNotIn("job_id", payload)
        self.assertNotIn("operation_id", payload)

    def test_no_credential_leakage_in_summary(self):
        payload = emit(
            Severity.WARNING,
            AlertCategory.BUDGET,
            f"Budget check failed with token={_META_LEAK_TOKEN}",
        )
        self.assertNotIn(_META_LEAK_TOKEN, payload["summary"])
        self.assertNotIn(_META_LEAK_TOKEN, json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
