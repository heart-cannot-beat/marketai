"""Import the legacy data/products JSON tree into the configured SQL database."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app import config
from app.adapters.persistence.json_importer import import_json_tree
from app.platform.tenancy import SYSTEM_MIGRATION, TenantContext, new_request_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Import MarketAI v1 JSON data")
    parser.add_argument("--source", type=Path, default=config.DATA_DIR, help="Legacy data root")
    parser.add_argument("--report", type=Path, default=None, help="Optional report output path")
    parser.add_argument(
        "--workspace",
        required=True,
        help="Explicit destination Workspace id",
    )
    parser.add_argument(
        "--initiator",
        required=True,
        help="Human or migration identity recorded for this import",
    )
    args = parser.parse_args()

    request_id = new_request_id()
    context = TenantContext(
        workspace_id=args.workspace,
        role="system",
        initiator_id=args.initiator,
        initiator_role="owner",
        executor=SYSTEM_MIGRATION,
        request_id=request_id,
        trace_id=request_id,
    )
    report = import_json_tree(context, args.source)
    report_path = args.report
    if report_path is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = config.DATA_DIR / "import-reports" / f"json-import-{timestamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), **report}, ensure_ascii=False, indent=2))
    if not report.get("ok", False):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
