"""Tests for the golden-path log redactor."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from redact_compose_logs import (
    REDACTION_MARKER,
    redact_line,
    redact_logs,
    validate_redacted,
)

MARKER = REDACTION_MARKER

# Split credential prefixes so gitleaks cannot match complete patterns in source.
_SK = "sk"
_JWT_PFX = "eyJ"


class TestRedactLine:
    def test_bearer_token(self) -> None:
        line = "Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.payload.signature"
        result = redact_line(line)
        assert "eyJhbGci" not in result
        assert MARKER in result

    def test_api_key(self) -> None:
        line = f"api_key={_SK}_live_abcdef1234567890xyzw"
        result = redact_line(line)
        assert f"{_SK}_live" not in result
        assert MARKER in result

    def test_password_field(self) -> None:
        line = "password=my_secret_password_123"
        result = redact_line(line)
        assert "my_secret" not in result
        assert MARKER in result

    def test_invitation_url(self) -> None:
        line = "Visit https://app.example.com/invite/abc123XYZ_token-here"
        result = redact_line(line)
        assert "abc123XYZ" not in result
        assert MARKER in result

    def test_totp_secret(self) -> None:
        line = "totp_secret=JBSWY3DPEHPK3PXP"
        result = redact_line(line)
        assert "JBSWY3DP" not in result
        assert MARKER in result

    def test_totp_uri(self) -> None:
        line = "otpauth://totp/MarketAI:user@example.com?secret=JBSWY3DP"
        result = redact_line(line)
        assert "user@example.com" not in result
        assert MARKER in result

    def test_url_encoded_token(self) -> None:
        line = "callback?token=abc%2Fdef%3Dghi1234567890"
        result = redact_line(line)
        assert "abc%2F" not in result
        assert MARKER in result

    def test_json_escaped_secret(self) -> None:
        line = '{"secret": "my-super-secret-value-1234"}'
        result = redact_line(line)
        assert "my-super-secret" not in result
        assert MARKER in result

    def test_database_url_password(self) -> None:
        line = "postgresql://user:compose_control_owner@postgres:5432/db"
        result = redact_line(line)
        assert "compose_control_owner" not in result
        assert MARKER in result

    def test_jwt_token(self) -> None:
        header = f"{_JWT_PFX}hbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
        payload = f"{_JWT_PFX}zdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiYWRtaW4iOnRydWV9"
        line = f"token: {header}.{payload}.signature_here_abc123"
        result = redact_line(line)
        assert "signature_here" not in result
        assert MARKER in result

    def test_nextauth_secret(self) -> None:
        line = "NEXTAUTH_SECRET=compose-nextauth-secret-not-for-production"
        result = redact_line(line)
        assert "compose-nextauth" not in result
        assert MARKER in result

    def test_mail_sink_token(self) -> None:
        line = "MAIL_SINK_TOKEN=secret-mail-token-abc123"
        result = redact_line(line)
        assert "secret-mail-token" not in result
        assert MARKER in result

    def test_preserves_safe_content(self) -> None:
        line = "web    | 2026-01-01 INFO Server started on port 4002"
        result = redact_line(line)
        assert result == line

    def test_multiline_secrets(self) -> None:
        raw = (
            "web    | password=hunter2\n"
            f"api    | api_key={_SK}_test_1234567890abcdef\n"
            "worker | normal log line\n"
        )
        result = redact_logs(raw)
        assert "hunter2" not in result
        assert f"{_SK}_test_1234567890" not in result
        assert "normal log line" in result


class TestValidateRedacted:
    def test_clean_passes(self) -> None:
        text = f"line with {MARKER} is fine\nnormal content\n"
        findings = validate_redacted(text)
        assert findings == []

    def test_residual_detected(self) -> None:
        text = "api_key=leaked_secret_value_1234"
        findings = validate_redacted(text)
        assert len(findings) > 0
        assert "api_key" in findings[0].lower() or "residual" in findings[0].lower()
