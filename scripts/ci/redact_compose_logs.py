#!/usr/bin/env python3
"""
Collect and redact Docker Compose logs for the golden-path E2E run.

Collects only web/api/worker logs from Compose start. Applies pattern-based
redaction for tokens, secrets, passwords, API keys, invitation URLs, TOTP
secrets, and recovery codes. On failure: deletes candidate, emits constant
marker. Uses atomic rename and second-pass validation scan.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SERVICES = ["web", "api", "worker"]

REDACTION_MARKER = "[REDACTED]"

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("bearer_token", re.compile(
        r"(?i)(Bearer\s+)[A-Za-z0-9\-_\.+/=]{8,}", re.IGNORECASE
    )),
    ("authorization_header", re.compile(
        r'(?i)((?:Authorization|X-Api-Key|X-Auth-Token)\s*[:=]\s*)[^\s"\']+',
    )),
    ("api_key", re.compile(
        r"(?i)((?:api[_-]?key|secret[_-]?key|access[_-]?token|private[_-]?key)"
        r"\s*[:=]\s*)[^\s\"',}]{8,}",
    )),
    ("password_field", re.compile(
        r"(?i)((?:password|passwd|pwd)\s*[:=]\s*)[^\s\"',}]+",
    )),
    ("invitation_url", re.compile(
        r"(/invite/)[A-Za-z0-9_\-]{8,}",
    )),
    ("invitation_token", re.compile(
        r"(?i)(invitation[_-]?(?:token|code|capability)\s*[:=]\s*)[^\s\"',}]{8,}",
    )),
    ("totp_secret", re.compile(
        r"(?i)((?:totp[_-]?secret|otp[_-]?secret|mfa[_-]?secret)\s*[:=]\s*)[^\s\"',}]+",
    )),
    ("totp_uri", re.compile(
        r"(otpauth://totp/)[^\s]+",
    )),
    ("recovery_code", re.compile(
        r"(?i)(recovery[_-]?code\s*[:=]\s*)[^\s\"',}]+",
    )),
    ("url_encoded_token", re.compile(
        r"(token=)[A-Za-z0-9%\-_\.+]{8,}",
    )),
    ("json_escaped_secret", re.compile(
        r'(?i)("(?:secret|token|password|api_key|access_token)":\s*")[^"]{8,}(")',
    )),
    ("nextauth_secret", re.compile(
        r"(?i)(NEXTAUTH_SECRET\s*=\s*)[^\s]+",
    )),
    ("database_url_password", re.compile(
        r"(://[^:]+:)[^@]+(@)",
    )),
    ("jwt_token", re.compile(
        r"(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.)[A-Za-z0-9_-]+",
    )),
    ("hex_secret", re.compile(
        r"(?i)((?:hmac|signing|encryption)[_-]?(?:key|secret)\s*[:=]\s*)[0-9a-f]{32,}",
    )),
    ("mail_sink_token", re.compile(
        r"(?i)(MAIL_SINK_TOKEN\s*[:=]\s*)[^\s]+",
    )),
]

VALIDATION_FAILURE_MARKER = "GOLDEN_PATH_LOG_REDACTION_FAILED"


def redact_line(line: str) -> str:
    """Apply all secret patterns to a single line."""
    for name, pattern in SECRET_PATTERNS:
        if name == "json_escaped_secret":
            line = pattern.sub(rf"\g<1>{REDACTION_MARKER}\g<2>", line)
        elif name == "database_url_password":
            line = pattern.sub(rf"\g<1>{REDACTION_MARKER}\g<2>", line)
        elif name == "jwt_token":
            line = pattern.sub(rf"\g<1>{REDACTION_MARKER}", line)
        else:
            line = pattern.sub(rf"\g<1>{REDACTION_MARKER}", line)
    return line


def collect_logs(
    project_name: str,
    compose_files: list[str],
    since: str | None = None,
) -> str:
    """Collect logs from compose services."""
    cmd = ["docker", "compose", "-p", project_name]
    for f in compose_files:
        cmd += ["-f", f]
    cmd += ["logs", "--no-color"]
    if since:
        cmd += ["--since", since]
    cmd += SERVICES

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"docker compose logs failed: {proc.stderr}")
    return proc.stdout


def redact_logs(raw: str) -> str:
    """Redact all secret patterns from log text."""
    lines = raw.split("\n")
    return "\n".join(redact_line(line) for line in lines)


def validate_redacted(text: str) -> list[str]:
    """Second-pass validation: scan for residual secrets."""
    findings: list[str] = []
    for i, line in enumerate(text.split("\n"), 1):
        for name, pattern in SECRET_PATTERNS:
            match = pattern.search(line)
            if match and REDACTION_MARKER not in match.group(0):
                findings.append(
                    f"Line {i}: residual '{name}' pattern detected"
                )
    return findings


def write_redacted_atomically(
    text: str,
    output_path: Path,
) -> None:
    """Write redacted logs with atomic rename."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=".redacted-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.rename(tmp_path, str(output_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Redact golden-path Compose logs")
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--compose-file", action="append", default=[])
    parser.add_argument("--since", default=None, help="Docker --since timestamp")
    parser.add_argument("--output", required=True, help="Output file path")
    parser.add_argument(
        "--raw-input",
        default=None,
        help="Read raw logs from file instead of docker compose",
    )
    args = parser.parse_args()

    output_path = Path(args.output)

    try:
        if args.raw_input:
            raw = Path(args.raw_input).read_text()
        else:
            raw = collect_logs(args.project_name, args.compose_file, args.since)

        redacted = redact_logs(raw)

        findings = validate_redacted(redacted)
        if findings:
            for f in findings:
                print(f"VALIDATION FAIL: {f}", file=sys.stderr)
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass
            marker_path = output_path.with_suffix(".FAILED")
            marker_path.write_text(VALIDATION_FAILURE_MARKER + "\n")
            return 1

        write_redacted_atomically(redacted, output_path)
        print(f"Redacted logs written to {output_path}")
        return 0

    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass
        marker_path = output_path.with_suffix(".FAILED")
        marker_path.write_text(VALIDATION_FAILURE_MARKER + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
