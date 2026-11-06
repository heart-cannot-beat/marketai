"""Migrate an immutable SQLite snapshot into an empty, migrated PostgreSQL DB."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app import config
from app.adapters.persistence.sqlite_postgres_migration import (
    migrate_sqlite_to_postgres,
    write_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy SQLite into PostgreSQL and verify counts, money and hashes"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=config.DATA_DIR / "marketai.db",
        help="SQLite file opened read-only; a private snapshot is upgraded instead",
    )
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    report = migrate_sqlite_to_postgres(args.source, config.DATABASE_URL)
    report_path = args.report
    if report_path is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = config.DATA_DIR / "import-reports" / f"sqlite-postgres-{timestamp}.json"
    write_report(report, report_path)
    print(json.dumps({"report": str(report_path), **report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
