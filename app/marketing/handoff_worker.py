from .blob_read import read_committed, write_generated
"""Build a real, immutable ZIP from exact committed blob versions."""
import csv
import hashlib
import io
import json
import zipfile
from dataclasses import asdict

from app.adapters.persistence.database import tenant_session
from app.platform.blob_store import CommittedBlobRef
from app.platform.operations import get_operation, transition_operation, OperationState, OperationResultRef
from . import export_text
from .workflow import scoped, get_round, handoff_quality_current
from .workflow_models import MarketingHandoffRecord as Handoff


def safe_cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value


def csv_bytes(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows([[safe_cell(value) for value in row] for row in rows])
    return stream.getvalue().encode("utf-8-sig")


async def package_bytes(manifest, blob_store):
    """The deterministic ZIP for one frozen manifest, read from exact committed blobs."""
    files, exported = {}, []
    total = 0
    for entry in manifest["entries"]:
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(entry["mime_type"])
        if not extension:
            raise ValueError("unsupported_export_mime")
        name = "media/" + entry["content_sha256"] + "." + extension
        if name not in files:
            content = await read_committed(blob_store, CommittedBlobRef(**entry["blob"]), 20 * 1024 * 1024)
            if hashlib.sha256(content).hexdigest() != entry["content_sha256"]:
                raise ValueError("export_content_changed")
            total += len(content)
            if total > 100 * 1024 * 1024:
                raise ValueError("export_too_large")
            files[name] = content
        exported.append({**{k: v for k, v in entry.items() if k != "blob"}, "file": name})
    # File names and stable keys are the same in every language; only MarketAI's words follow it.
    language = export_text.package_language(manifest)
    export_manifest = {**manifest, "language": language, "entries": exported}
    files["manifest.json"] = json.dumps(export_manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
    files["ads.csv"] = csv_bytes(*export_text.ads_rows(exported, language))
    files["daily-metrics-template.csv"] = csv_bytes(export_text.METRICS_TEMPLATE_COLUMNS, [])
    files["README.txt"] = export_text.readme(manifest, language).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return output.getvalue()


async def execute_export(ctx, handoff_id, blob_store):
    with tenant_session(ctx) as session:
        rows = scoped(session, ctx, Handoff, id=handoff_id)
        if not rows:
            raise ValueError("handoff_not_found")
        handoff = rows[0]
        if handoff.state in ("ready", "failed"):
            return
        manifest, operation_id = handoff.manifest_json, handoff.operation_id
        if get_operation(session, operation_id).state == "queued":
            transition_operation(session, operation_id, OperationState.RUNNING, "export_started", ctx.executor)
    try:
        with tenant_session(ctx) as session:
            row = get_round(session, ctx, manifest["round_id"])
            if not handoff_quality_current(session, ctx, row, handoff):
                raise ValueError("quality_review_changed")
        content = await package_bytes(manifest, blob_store)
        ref = await write_generated(blob_store, f"committed/{ctx.workspace_id}/marketing/{handoff_id}.zip", content, "application/zip")
        with tenant_session(ctx) as session:
            handoff = scoped(session, ctx, Handoff, id=handoff_id)[0]
            if handoff.state == "ready":
                return
            row = get_round(session, ctx, manifest["round_id"])
            if not handoff_quality_current(session, ctx, row, handoff):
                raise ValueError("quality_review_changed")
            handoff.blob_json, handoff.state = asdict(ref), "ready"
            transition_operation(session, operation_id, OperationState.SUCCEEDED, "export_completed", ctx.executor,
                result_ref=OperationResultRef(resource_type="marketing_handoff", resource_id=handoff_id))
    except Exception:
        # Domain state is explicit; deterministic ZIP writing is crash safe.
        with tenant_session(ctx) as session:
            handoff = scoped(session, ctx, Handoff, id=handoff_id)[0]
            if handoff.state != "ready":
                handoff.state, handoff.error_code = "failed", "export_build_failed"
                transition_operation(session, operation_id, OperationState.FAILED, "export_build_failed", ctx.executor)
        raise
