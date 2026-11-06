"""The generic audit contract attributes both actors and never stores secrets."""
from __future__ import annotations

import json
import unittest

from app.platform.audit import REDACTED, audit_log_context, redact
from app.platform.tenancy import TenantContext


class AuditRedactionTests(unittest.TestCase):
    def context(self) -> TenantContext:
        return TenantContext(
            workspace_id="ws_audit",
            role="member",
            initiator_id="user_audit",
            executor="system:worker",
            request_id="req_audit",
            trace_id="trace_audit",
            authorization_epoch=9,
        )

    def test_nested_credentials_payment_data_and_urls_are_redacted(self) -> None:
        payload = {
            "access_token": "top-secret-token",
            "nested": {
                "api-key": "byok-value",
                "card_number": "4111111111111111",
                "safe": "retained",
            },
            "url": (
                "https://alice:userinfo-secret@example.test/page?cursor=ok&appsecret_proof=proof-value"
                "&X-Amz-Signature=signed-value#fragment-secret"
            ),
            "signed_url": "https://blob.test/private?opaque=still-secret",
            "authorization_header": "Bearer bearer-value",
            "message": "token eyJhbGciOiJIUzI1NiJ9.cGF5bG9hZA.c2ln must not survive",
        }

        safe = redact(payload)
        encoded = json.dumps(safe, sort_keys=True)

        for secret in (
            "top-secret-token",
            "byok-value",
            "4111111111111111",
            "proof-value",
            "signed-value",
            "still-secret",
            "alice",
            "userinfo-secret",
            "fragment-secret",
            "bearer-value",
            "eyJhbGciOiJIUzI1NiJ9.cGF5bG9hZA.c2ln",
        ):
            self.assertNotIn(secret, encoded)
        self.assertIn("retained", encoded)
        self.assertIn("cursor=ok", safe["url"])
        self.assertIn(REDACTED, encoded)

    def test_relative_and_common_signed_url_parameters_are_redacted(self) -> None:
        safe = redact(
            {
                "relative_url": "/blob/object?sig=relative-secret&apiKey=api-secret&cursor=ok",
                "absolute_url": "https://blob.test/object?sig=absolute-secret&apiKey=other-secret",
            }
        )

        encoded = json.dumps(safe, sort_keys=True)
        for secret in (
            "relative-secret",
            "api-secret",
            "absolute-secret",
            "other-secret",
        ):
            self.assertNotIn(secret, encoded)
        self.assertIn("cursor=ok", safe["relative_url"])

    def test_structured_context_preserves_both_actors_and_trace_identity(self) -> None:
        fields = audit_log_context(
            self.context(),
            event_type="job.succeeded",
            resource_type="job",
            resource_id="job_1",
            details={"password": "nope", "attempt": 2},
        )

        self.assertEqual(fields["workspace_id"], "ws_audit")
        self.assertEqual(fields["initiator_id"], "user_audit")
        self.assertEqual(fields["initiator_role"], "member")
        self.assertEqual(fields["executor"], "system:worker")
        self.assertEqual(fields["request_id"], "req_audit")
        self.assertEqual(fields["trace_id"], "trace_audit")
        self.assertEqual(fields["authorization_epoch"], 9)
        self.assertEqual(fields["details"], {"password": REDACTED, "attempt": 2})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
