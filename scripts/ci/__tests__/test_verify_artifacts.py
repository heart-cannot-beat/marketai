"""Tests for the golden-path artifact verifier."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verify_golden_artifacts import (
    check_allowlist,
    check_completeness,
    check_secrets,
    check_size_bounds,
    check_structural_safety,
    verify_artifacts,
)


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    """Create a minimal valid artifact directory."""
    report = tmp_path / "golden-path-report.json"
    report.write_text(json.dumps({"version": 1, "status": "passed"}))
    return tmp_path


class TestStructuralSafety:
    def test_rejects_symlinks(self, artifact_dir: Path) -> None:
        link = artifact_dir / "bad-link.json"
        link.symlink_to(artifact_dir / "golden-path-report.json")
        findings = check_structural_safety(artifact_dir)
        assert any(f.rule == "symlink" for f in findings)

    def test_rejects_hidden_files(self, artifact_dir: Path) -> None:
        hidden = artifact_dir / ".secret"
        hidden.write_text("hidden")
        findings = check_structural_safety(artifact_dir)
        assert any(f.rule == "hidden-file" for f in findings)

    def test_accepts_normal_files(self, artifact_dir: Path) -> None:
        findings = check_structural_safety(artifact_dir)
        assert not findings


class TestAllowlist:
    def test_rejects_unlisted_file(self, artifact_dir: Path) -> None:
        bad = artifact_dir / "malware.exe"
        bad.write_text("payload")
        findings = check_allowlist(artifact_dir)
        assert any(f.rule == "not-allowed" for f in findings)

    def test_accepts_valid_report(self, artifact_dir: Path) -> None:
        findings = check_allowlist(artifact_dir)
        assert not findings

    def test_accepts_screenshot(self, artifact_dir: Path) -> None:
        ss_dir = artifact_dir / "screenshots"
        ss_dir.mkdir()
        (ss_dir / "step-01-library.png").write_bytes(b"\x89PNG")
        findings = check_allowlist(artifact_dir)
        assert not findings

    def test_rejects_unexpected_extension(self, artifact_dir: Path) -> None:
        bad = artifact_dir / "evidence" / "data.xml"
        bad.parent.mkdir()
        bad.write_text("<data/>")
        findings = check_allowlist(artifact_dir)
        assert any(f.rule == "not-allowed" for f in findings)


class TestSizeBounds:
    def test_rejects_oversized_file(self, artifact_dir: Path) -> None:
        big = artifact_dir / "golden-path-evidence.json"
        big.write_text("x" * (11 * 1024 * 1024))
        findings = check_size_bounds(artifact_dir)
        assert any(f.rule == "oversized" for f in findings)

    def test_accepts_normal_size(self, artifact_dir: Path) -> None:
        findings = check_size_bounds(artifact_dir)
        assert not findings


class TestCompleteness:
    def test_missing_required(self, tmp_path: Path) -> None:
        findings = check_completeness(tmp_path)
        assert any(f.rule == "missing" for f in findings)
        assert any("golden-path-report.json" in f.file for f in findings)

    def test_all_present(self, artifact_dir: Path) -> None:
        findings = check_completeness(artifact_dir)
        assert not findings


class TestSecretScan:
    def test_detects_bearer_token(self, artifact_dir: Path) -> None:
        (artifact_dir / "evidence" ).mkdir()
        bad = artifact_dir / "evidence" / "leaked.json"
        bad.write_text(json.dumps({
            "token": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
        }))
        findings = check_secrets(artifact_dir)
        assert len(findings) > 0

    def test_detects_invitation_path(self, artifact_dir: Path) -> None:
        report = artifact_dir / "golden-path-report.json"
        report.write_text(json.dumps({
            "leaked": "/invite/secrettoken123ABC"
        }))
        findings = check_secrets(artifact_dir)
        assert any("invitation" in f.rule for f in findings)

    def test_detects_database_password(self, artifact_dir: Path) -> None:
        log_dir = artifact_dir / "logs"
        log_dir.mkdir()
        log_file = log_dir / "api-startup.log"
        log_file.write_text("postgresql://user:super_secret_password@postgres:5432/db")
        findings = check_secrets(artifact_dir)
        assert len(findings) > 0

    def test_accepts_explicitly_redacted_database_password(
        self, artifact_dir: Path
    ) -> None:
        log_dir = artifact_dir / "logs"
        log_dir.mkdir()
        log_file = log_dir / "api-startup.log"
        log_file.write_text("postgresql://user:[REDACTED]@postgres:5432/db")
        findings = check_secrets(artifact_dir)
        assert not findings

    def test_does_not_join_safe_urls_across_log_lines(
        self, artifact_dir: Path
    ) -> None:
        log_dir = artifact_dir / "logs"
        log_dir.mkdir()
        log_file = log_dir / "api-startup.log"
        log_file.write_text(
            "ready at http://localhost:4002/health\n"
            "workspace owner: owner@example.test\n"
        )
        findings = check_secrets(artifact_dir)
        assert not findings

    def test_clean_artifacts_pass(self, artifact_dir: Path) -> None:
        findings = check_secrets(artifact_dir)
        assert not findings


class TestEndToEnd:
    def test_valid_artifacts_pass(self, artifact_dir: Path) -> None:
        findings = verify_artifacts(artifact_dir)
        assert not findings

    def test_multiple_violations_reported(self, artifact_dir: Path) -> None:
        (artifact_dir / ".hidden").write_text("x")
        (artifact_dir / "bad.exe").write_text("payload")
        findings = verify_artifacts(artifact_dir)
        assert len(findings) >= 2
