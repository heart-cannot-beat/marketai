#!/usr/bin/env python3
"""
Verify golden-path E2E artifacts before CI upload.

Checks: allowlist, JSON schema validation, secret scan, size bounds,
completeness, structural safety (reject symlinks, hidden files, path
traversal). Verifier failure means artifacts may contain secrets —
CI must never upload on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

MAX_FILE_SIZE_MB = 10
MAX_TOTAL_SIZE_MB = 50

REQUIRED_ARTIFACTS = [
    "golden-path-report.json",
]

ALLOWED_EXTENSIONS = frozenset({
    ".json",
    ".png",
    ".txt",
    ".log",
})

ALLOWED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^golden-path-report\.json$"),
    re.compile(r"^golden-path-evidence\.json$"),
    re.compile(r"^acceptance-manifest\.json$"),
    re.compile(r"^screenshots/[a-z0-9_-]+\.png$"),
    re.compile(r"^logs/[a-z0-9_-]+\.log$"),
    re.compile(r"^logs/[a-z0-9_-]+\.txt$"),
    re.compile(r"^evidence/[a-z0-9_-]+\.json$"),
]

SECRET_SCAN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("bearer_token", re.compile(
        r"(?i)Bearer\s+[A-Za-z0-9\-_\.+/=]{20,}"
    )),
    ("api_key_value", re.compile(
        r"(?i)(?:api[_-]?key|secret[_-]?key|access[_-]?token|private[_-]?key)"
        r"\s*[:=]\s*[^\s\"',}]{20,}"
    )),
    ("password_value", re.compile(
        r"(?i)(?:password|passwd)\s*[:=]\s*[^\s\"',}]{8,}"
    )),
    ("invitation_path", re.compile(
        r"/invite/[A-Za-z0-9_\-]{8,}"
    )),
    ("totp_secret", re.compile(
        r"(?i)(?:totp[_-]?secret|otp[_-]?secret)\s*[:=]\s*[^\s\"',}]+"
    )),
    ("totp_uri", re.compile(
        r"otpauth://totp/"
    )),
    ("jwt_token", re.compile(
        r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"
    )),
    ("nextauth_secret", re.compile(
        r"(?i)NEXTAUTH_SECRET\s*=\s*\S+"
    )),
    ("database_url_with_password", re.compile(
        r"://[^:\s/]+:(?!\[REDACTED\]@)[^@\s]{4,}@"
    )),
    ("recovery_code", re.compile(
        r"(?i)recovery[_-]?code\s*[:=]\s*[^\s\"',}]+"
    )),
    ("mail_sink_token", re.compile(
        r"(?i)MAIL_SINK_TOKEN\s*[:=]\s*\S+"
    )),
]

VALIDATION_FAILURE_MARKER = "GOLDEN_PATH_ARTIFACT_VERIFICATION_FAILED"


class Finding:
    def __init__(self, file: str, rule: str, detail: str) -> None:
        self.file = file
        self.rule = rule
        self.detail = detail

    def __str__(self) -> str:
        return f"[{self.file}] {self.rule}: {self.detail}"


def check_structural_safety(
    artifact_dir: Path,
) -> list[Finding]:
    """Reject symlinks, hidden files, path traversal."""
    findings: list[Finding] = []
    resolved_root = artifact_dir.resolve()

    for item in artifact_dir.rglob("*"):
        rel = item.relative_to(artifact_dir)
        rel_str = str(rel)

        if item.is_symlink():
            findings.append(Finding(rel_str, "symlink", "Symlinks forbidden"))
            continue

        if any(part.startswith(".") for part in rel.parts):
            findings.append(Finding(rel_str, "hidden-file", "Hidden files forbidden"))
            continue

        if ".." in rel.parts:
            findings.append(Finding(rel_str, "path-traversal", "Path traversal forbidden"))
            continue

        if not item.resolve().is_relative_to(resolved_root):
            findings.append(Finding(rel_str, "escape", "File escapes artifact directory"))

    return findings


def check_allowlist(
    artifact_dir: Path,
) -> list[Finding]:
    """Reject files not matching the allowlist."""
    findings: list[Finding] = []

    for item in artifact_dir.rglob("*"):
        if item.is_dir():
            continue
        rel = str(item.relative_to(artifact_dir))
        if not any(p.match(rel) for p in ALLOWED_PATTERNS):
            findings.append(Finding(rel, "not-allowed", f"Not in allowlist"))

    return findings


def check_size_bounds(
    artifact_dir: Path,
) -> list[Finding]:
    """Check individual and total file size bounds."""
    findings: list[Finding] = []
    total = 0

    for item in artifact_dir.rglob("*"):
        if item.is_dir():
            continue
        size = item.stat().st_size
        total += size
        size_mb = size / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            findings.append(Finding(
                str(item.relative_to(artifact_dir)),
                "oversized",
                f"{size_mb:.1f}MB exceeds {MAX_FILE_SIZE_MB}MB limit",
            ))

    total_mb = total / (1024 * 1024)
    if total_mb > MAX_TOTAL_SIZE_MB:
        findings.append(Finding(
            "(total)",
            "total-oversized",
            f"{total_mb:.1f}MB exceeds {MAX_TOTAL_SIZE_MB}MB limit",
        ))

    return findings


def check_completeness(
    artifact_dir: Path,
) -> list[Finding]:
    """Check required artifacts are present."""
    findings: list[Finding] = []
    for required in REQUIRED_ARTIFACTS:
        if not (artifact_dir / required).exists():
            findings.append(Finding(required, "missing", "Required artifact missing"))
    return findings


def scan_secrets_in_file(
    file_path: Path,
    rel_path: str,
) -> list[Finding]:
    """Scan a text file for secret patterns."""
    findings: list[Finding] = []
    try:
        text = file_path.read_text(errors="replace")
    except Exception:
        return findings

    for name, pattern in SECRET_SCAN_PATTERNS:
        if pattern.search(text):
            findings.append(Finding(
                rel_path,
                f"secret-{name}",
                f"Secret pattern '{name}' detected",
            ))

    return findings


def check_secrets(
    artifact_dir: Path,
) -> list[Finding]:
    """Scan all text files for secret patterns."""
    findings: list[Finding] = []
    text_extensions = {".json", ".txt", ".log"}

    for item in artifact_dir.rglob("*"):
        if item.is_dir():
            continue
        if item.suffix in text_extensions:
            rel = str(item.relative_to(artifact_dir))
            findings.extend(scan_secrets_in_file(item, rel))

    return findings


def validate_json_schemas(
    artifact_dir: Path,
    schema_dir: Path | None,
) -> list[Finding]:
    """Validate JSON artifacts against schemas if available."""
    findings: list[Finding] = []
    if not schema_dir or not schema_dir.exists():
        return findings

    schema_map: dict[str, str] = {
        "golden-path-report.json": "golden-path-report.schema.json",
        "golden-path-evidence.json": "golden-path-evidence.schema.json",
        "acceptance-manifest.json": "acceptance-manifest.schema.json",
    }

    for artifact_name, schema_name in schema_map.items():
        artifact_path = artifact_dir / artifact_name
        schema_path = schema_dir / schema_name
        if not artifact_path.exists() or not schema_path.exists():
            continue

        try:
            artifact_data = json.loads(artifact_path.read_text())
            schema_data = json.loads(schema_path.read_text())
            _validate_basic_schema(artifact_data, schema_data, artifact_name, findings)
        except json.JSONDecodeError as e:
            findings.append(Finding(artifact_name, "invalid-json", str(e)))

    return findings


def _validate_basic_schema(
    data: Any,
    schema: dict[str, Any],
    file_name: str,
    findings: list[Finding],
) -> None:
    """Basic JSON schema validation (type and required fields)."""
    schema_type = schema.get("type")
    if schema_type == "object" and not isinstance(data, dict):
        findings.append(Finding(file_name, "schema-type", f"Expected object, got {type(data).__name__}"))
        return

    if schema_type == "object" and isinstance(data, dict):
        for req in schema.get("required", []):
            if req not in data:
                findings.append(Finding(file_name, "schema-required", f"Missing required field: {req}"))


def verify_artifacts(
    artifact_dir: Path,
    schema_dir: Path | None = None,
) -> list[Finding]:
    """Run all verification checks."""
    findings: list[Finding] = []
    findings.extend(check_structural_safety(artifact_dir))
    findings.extend(check_allowlist(artifact_dir))
    findings.extend(check_size_bounds(artifact_dir))
    findings.extend(check_completeness(artifact_dir))
    findings.extend(check_secrets(artifact_dir))
    findings.extend(validate_json_schemas(artifact_dir, schema_dir))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify golden-path artifacts")
    parser.add_argument("--artifact-dir", required=True, help="Artifacts directory")
    parser.add_argument("--schema-dir", default=None, help="JSON schema directory")
    args = parser.parse_args()

    artifact_dir = Path(args.artifact_dir)
    if not artifact_dir.is_dir():
        print(f"Artifact directory does not exist: {artifact_dir}", file=sys.stderr)
        return 1

    schema_dir = Path(args.schema_dir) if args.schema_dir else None
    findings = verify_artifacts(artifact_dir, schema_dir)

    if findings:
        for f in findings:
            print(f"FAIL {f}", file=sys.stderr)
        print(VALIDATION_FAILURE_MARKER)
        return 1

    print("All artifact checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
