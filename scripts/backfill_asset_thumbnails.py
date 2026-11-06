#!/usr/bin/env python3
"""Backfill gallery thumbnails for one Workspace's existing AssetVersions (Issue #159).

Thin CLI over ``app.generation.thumbnail_backfill`` -- idempotent and
resumable, one Workspace per ``--workspace-id`` (repeatable), ``--dry-run``
to count candidates first, ``--limit`` for a cautious first pass. Needs the
same ``MARKETAI_DATABASE_URL`` / ``MARKETAI_BLOB_STORE_*`` environment the
api/worker process runs with; the Railway images do not ship ``scripts/``,
so there run ``python -m app.generation.thumbnail_backfill`` instead, with
the same arguments.

    python scripts/backfill_asset_thumbnails.py --workspace-id <uuid> --dry-run
    python scripts/backfill_asset_thumbnails.py --workspace-id <uuid>

Exit status 1 when any version could not be derived (it stays null and is
retried by the next run); the JSON report on stdout lists the failures.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.generation.thumbnail_backfill import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
