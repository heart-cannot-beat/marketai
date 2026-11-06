"""Suite-wide isolated environment bootstrap.

Pytest imports this file before collecting test modules.  Database-backed
modules therefore cannot bind ``app.config`` to the repository's development
database merely because their filename sorts before ``test_foundation.py``.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest


TEST_ROOT = Path(tempfile.mkdtemp(prefix="marketai-pytest-"))
atexit.register(shutil.rmtree, TEST_ROOT, ignore_errors=True)

os.environ["MARKETAI_TEST_ROOT"] = str(TEST_ROOT)
os.environ["MARKETAI_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["MARKETAI_DATABASE_URL"] = f"sqlite:///{TEST_ROOT / 'marketai.db'}"
os.environ["MARKETAI_WORKSPACE_ID"] = "ws_test"
os.environ["MAX_COST_PER_RUN"] = "5"
os.environ["MAX_COST_PER_DAY"] = "20"
os.environ["JOB_LEASE_SECONDS"] = "1"


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "postgres: PostgreSQL-specific test (RLS enforcement)")
    config.addinivalue_line(
        "markers", "e2e: end-to-end browser test (Playwright + a running server; CI-only)"
    )


def pytest_collection_modifyitems(config, items):
    """Skip postgres- and e2e-marked tests in development (SQLite-only,
    browser-less local/CI-lite environment)."""
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="PostgreSQL RLS tests skipped in SQLite environment"))
        if "e2e" in item.keywords:
            item.add_marker(
                pytest.mark.skip(reason="Playwright browser E2E tests require a running server; CI-only")
            )
