"""Dev-only local blob transfer storage process (spec §7.4).

`LocalBlobTransferApp` stands in for S3 in local development: it is a
separate, minimal ASGI application that performs the actual binary transfer
for `LocalFileBlobStore` (`app/adapters/blob_store_local.py`). It is never
imported or mounted by the FastAPI Domain API process (`app/main.py`) --
ADR-0004 rejects browser access to FastAPI, and this process occupies the
object-storage role S3 occupies in production. It has no domain routers, no
database session, and no delegated JWT middleware; the HMAC signature in the
URL path is the sole authorization mechanism, exactly as a signed S3 URL is.

Run directly for local development:

    python -m worker.local_blob_transfer

which starts uvicorn on `MARKETAI_BLOB_STORE_LOCAL_PORT` (default 9000).
Production refuses to start this process
(`assert_local_blob_transfer_boundary`).

Route: `GET|PUT /_local_blob/{signature}/{key:path}`. See
`app/adapters/blob_store_local.py` for how the signature is computed
(`compute_local_signature`) -- it binds the method, key, expiry, and (for
uploads) the content constraints, so a download URL cannot be replayed as an
upload and an upload cannot smuggle bytes outside its declared constraints.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from app.adapters.blob_store_local import (
    advisory_lock,
    compute_local_signature,
    compute_revision,
    lock_path_for,
    resolve_key_path,
    sidecar_path,
)

#: Route prefix. `{signature}` is a hex-encoded HMAC digest; `{key}` is the
#: full `staging/...` or `committed/...` object key (path-valued).
_ROUTE_PREFIX = "/_local_blob/"

_GET_QUERY_KEYS = frozenset({"expires", "v"})
_PUT_QUERY_KEYS = frozenset({"expires", "ct", "cl_min", "cl_max", "cs"})

_STREAM_CHUNK = 1024 * 1024

_ALLOWED_METHODS = "GET, PUT, OPTIONS"
_ALLOWED_HEADERS = "Content-Type, x-amz-checksum-sha256"


def assert_local_blob_transfer_boundary() -> None:
    """Fail startup, not the first request, if this dev-only process is
    pointed at a production (PostgreSQL) database.

    Mirrors `assert_local_identity_boundary` in
    `app/adapters/persistence/database.py`: the dialect name is checked, not
    a URL prefix or an env flag, so it also catches
    ``postgresql+psycopg://``/``postgresql+asyncpg://`` variants. This
    process has no tenancy or authentication of its own; it must never be
    reachable against a real multi-tenant database.

    The `engine` import is deferred to this call rather than made at module
    level: `app.adapters.persistence.database` creates its SQLAlchemy engine
    singleton, bound to `config.DATABASE_URL`, as an import-time side
    effect. Merely importing this ASGI app (e.g. to construct it in a test)
    must not force that -- and must never race a test harness that sets
    `MARKETAI_DATABASE_URL` before importing the database module itself.
    """
    from app.adapters.persistence.database import engine

    if engine.dialect.name == "postgresql":
        raise RuntimeError(
            "LocalBlobTransferApp refuses to start against a PostgreSQL "
            "database; it is a dev-only stand-in for S3 with no delegated "
            "identity, tenancy, or authentication of its own."
        )


def _cors_headers(scope: dict[str, Any], cors_origin: str) -> list[tuple[bytes, bytes]]:
    """CORS headers matching S3 bucket CORS (spec §7.4): exact origin match,
    no wildcard, `Vary: Origin`, no credentialed CORS (the signed capability
    is the credential).
    """
    origin = b""
    for name, value in scope.get("headers", []):
        if name == b"origin":
            origin = value
            break
    headers = [(b"vary", b"Origin")]
    if origin == cors_origin.encode("utf-8"):
        headers.append((b"access-control-allow-origin", origin))
        headers.append((b"access-control-allow-methods", _ALLOWED_METHODS.encode("utf-8")))
        headers.append((b"access-control-allow-headers", _ALLOWED_HEADERS.encode("utf-8")))
    return headers


def _get_header(scope: dict[str, Any], name: bytes) -> bytes | None:
    for header_name, value in scope.get("headers", []):
        if header_name == name:
            return value
    return None


def _parse_exact_query(query_string: bytes, expected_keys: frozenset[str]) -> dict[str, str] | None:
    """Parse `query_string` requiring exactly `expected_keys`, no duplicates.

    Spec §7.4 transfer enforcement step 1: "exact non-duplicated query
    fields" -- the canonical HMAC input can never differ from the values
    this parser hands back to the caller.
    """
    raw = query_string.decode("utf-8", "strict") if query_string else ""
    if not raw:
        return None if expected_keys else {}
    values: dict[str, str] = {}
    for piece in raw.split("&"):
        if "=" not in piece:
            return None
        key, _, value = piece.partition("=")
        key = _unquote(key)
        value = _unquote(value)
        if key in values:
            return None
        values[key] = value
    if set(values.keys()) != expected_keys:
        return None
    return values


def _unquote(value: str) -> str:
    return urllib.parse.unquote_plus(value)


class LocalBlobTransferApp:
    """Storage-only ASGI application (spec §7.4)."""

    def __init__(self, *, root: Path, signing_secret: bytes, cors_origin: str) -> None:
        if not signing_secret:
            raise ValueError("LocalBlobTransferApp requires a non-empty signing secret")
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._signing_secret = signing_secret
        self._cors_origin = cors_origin

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if scope["type"] != "http":
            return
        method = scope["method"]
        path = scope["path"]
        trace_id = f"trc_{uuid.uuid4().hex}"
        cors_headers = _cors_headers(scope, self._cors_origin)

        if method == "GET" and path == "/health":
            await self._send_empty(send, 200, cors_headers)
            return

        if method == "OPTIONS":
            await self._send_empty(send, 204, cors_headers)
            return

        if not path.startswith(_ROUTE_PREFIX):
            await self._send_problem(send, 404, "not_found", "Not Found", trace_id, False, cors_headers)
            return
        remainder = path[len(_ROUTE_PREFIX):]
        signature, sep, key = remainder.partition("/")
        if not sep or not signature or not key:
            await self._send_problem(send, 404, "not_found", "Not Found", trace_id, False, cors_headers)
            return

        if method == "GET":
            await self._handle_get(send, signature, key, scope.get("query_string", b""), trace_id, cors_headers)
        elif method == "PUT":
            await self._handle_put(
                receive, send, signature, key, scope, scope.get("query_string", b""), trace_id, cors_headers
            )
        else:
            await self._send_problem(
                send, 405, "method_not_allowed", "Method Not Allowed", trace_id, False, cors_headers
            )

    # -- GET (download) ---------------------------------------------------

    async def _handle_get(
        self,
        send,
        signature: str,
        key: str,
        query_string: bytes,
        trace_id: str,
        cors_headers: list[tuple[bytes, bytes]],
    ) -> None:
        query = _parse_exact_query(query_string, _GET_QUERY_KEYS)
        if query is None:
            await self._send_problem(send, 400, "invalid_query", "Invalid query parameters", trace_id, False, cors_headers)
            return
        try:
            expiry = int(query["expires"])
        except ValueError:
            await self._send_problem(send, 400, "invalid_query", "Invalid expiry", trace_id, False, cors_headers)
            return
        storage_revision = query["v"]
        if not storage_revision:
            await self._send_problem(send, 400, "invalid_query", "Missing storage revision", trace_id, False, cors_headers)
            return

        expected_signature = compute_local_signature(
            self._signing_secret, "GET", key, expiry, storage_revision=storage_revision
        )
        if not _constant_time_eq(expected_signature, signature):
            await self._send_problem(send, 403, "invalid_signature", "Invalid signature", trace_id, False, cors_headers)
            return
        if expiry < int(time.time()):
            await self._send_problem(send, 410, "signature_expired", "Signed URL expired", trace_id, False, cors_headers)
            return

        try:
            resolved = resolve_key_path(self._root, key)
        except ValueError:
            await self._send_problem(send, 404, "object_not_found", "Object not found", trace_id, False, cors_headers)
            return
        try:
            st = os.stat(resolved, follow_symlinks=False)
        except FileNotFoundError:
            await self._send_problem(send, 404, "object_not_found", "Object not found", trace_id, False, cors_headers)
            return
        if compute_revision(st) != storage_revision:
            # The current generation no longer matches the signed revision
            # (spec §7.4 transfer enforcement step 5) -- treated as a miss,
            # never served.
            await self._send_problem(send, 404, "object_not_found", "Object not found", trace_id, False, cors_headers)
            return

        headers = [
            (b"content-type", b"application/octet-stream"),
            (b"content-length", str(st.st_size).encode("ascii")),
            *cors_headers,
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        with open(resolved, "rb") as handle:
            while True:
                chunk = handle.read(_STREAM_CHUNK)
                if not chunk:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    break
                await send({"type": "http.response.body", "body": chunk, "more_body": True})

    # -- PUT (upload) -------------------------------------------------------

    async def _handle_put(
        self,
        receive,
        send,
        signature: str,
        key: str,
        scope: dict[str, Any],
        query_string: bytes,
        trace_id: str,
        cors_headers: list[tuple[bytes, bytes]],
    ) -> None:
        query = _parse_exact_query(query_string, _PUT_QUERY_KEYS)
        if query is None:
            await self._send_problem(send, 400, "invalid_query", "Invalid query parameters", trace_id, False, cors_headers)
            return
        try:
            expiry = int(query["expires"])
            content_length_min = int(query["cl_min"])
            content_length_max = int(query["cl_max"])
        except ValueError:
            await self._send_problem(send, 400, "invalid_query", "Invalid query parameters", trace_id, False, cors_headers)
            return
        content_type = query["ct"]
        checksum_expected = query["cs"]
        if not content_type or not checksum_expected:
            await self._send_problem(send, 400, "invalid_query", "Missing content constraints", trace_id, False, cors_headers)
            return

        expected_signature = compute_local_signature(
            self._signing_secret,
            "PUT",
            key,
            expiry,
            content_type=content_type,
            content_length_min=content_length_min,
            content_length_max=content_length_max,
            checksum_sha256_b64=checksum_expected,
        )
        if not _constant_time_eq(expected_signature, signature):
            await self._drain(receive)
            await self._send_problem(send, 403, "invalid_signature", "Invalid signature", trace_id, False, cors_headers)
            return
        if expiry < int(time.time()):
            await self._drain(receive)
            await self._send_problem(send, 410, "signature_expired", "Signed URL expired", trace_id, False, cors_headers)
            return

        # Step 3 (spec §7.4): the signed Content-Type and checksum must be
        # exactly the values the request actually carries -- not just the
        # values it claims in the signed query.
        request_content_type = (_get_header(scope, b"content-type") or b"").split(b";")[0].strip().decode("latin-1")
        request_checksum = (_get_header(scope, b"x-amz-checksum-sha256") or b"").decode("latin-1")
        if request_content_type != content_type or request_checksum != checksum_expected:
            await self._drain(receive)
            await self._send_problem(
                send, 400, "content_constraint_mismatch", "Content-Type or checksum header mismatch",
                trace_id, False, cors_headers,
            )
            return

        try:
            target_path = resolve_key_path(self._root, key)
        except ValueError:
            await self._drain(receive)
            await self._send_problem(send, 400, "invalid_key", "Invalid object key", trace_id, False, cors_headers)
            return

        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.parent / f".{target_path.name}.{uuid.uuid4().hex}.tmp"
        checksum = hashlib.sha256()
        size = 0
        ok = True
        try:
            with open(tmp_path, "wb") as tmp:
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        ok = False
                        break
                    body = message.get("body", b"")
                    if body:
                        size += len(body)
                        if size > content_length_max:
                            ok = False
                            await self._drain(receive)
                            await self._send_problem(
                                send, 400, "content_too_large", "Upload exceeds declared maximum size",
                                trace_id, False, cors_headers,
                            )
                            break
                        checksum.update(body)
                        tmp.write(body)
                    if not message.get("more_body", False):
                        break
            if not ok:
                return
            if size < content_length_min:
                await self._send_problem(
                    send, 400, "content_too_small", "Upload is smaller than declared minimum size",
                    trace_id, False, cors_headers,
                )
                return
            actual_checksum = _b64_digest(checksum)
            if actual_checksum != checksum_expected:
                await self._send_problem(
                    send, 400, "checksum_mismatch", "Uploaded bytes do not match declared checksum",
                    trace_id, False, cors_headers,
                )
                return

            fd = os.open(tmp_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            lock_file = lock_path_for(self._root, key)
            with advisory_lock(lock_file):
                os.replace(tmp_path, target_path)
                sidecar = sidecar_path(target_path)
                _write_sidecar(sidecar, content_type=content_type, checksum_sha256_b64=checksum_expected)
            tmp_path = None  # committed; nothing left to clean up
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except FileNotFoundError:
                    pass

        if ok:
            headers = [(b"content-length", b"0"), *cors_headers]
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _drain(self, receive) -> None:
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect" or not message.get("more_body", False):
                    return
        except Exception:
            return

    # -- response helpers ---------------------------------------------------

    async def _send_empty(self, send, status: int, cors_headers: list[tuple[bytes, bytes]]) -> None:
        await send({"type": "http.response.start", "status": status, "headers": cors_headers})
        await send({"type": "http.response.body", "body": b""})

    async def _send_problem(
        self,
        send,
        status: int,
        code: str,
        title: str,
        trace_id: str,
        retryable: bool,
        cors_headers: list[tuple[bytes, bytes]],
    ) -> None:
        # No filesystem path or signed URL ever appears in a response body
        # (spec §7.4 transfer enforcement, Global Constraint on signed URLs
        # never appearing in logs applies equally to responses).
        body = json.dumps(
            {
                "type": f"urn:marketai:error:{code.replace('_', '-')}",
                "title": title,
                "status": status,
                "code": code,
                "trace_id": trace_id,
                "retryable": retryable,
            }
        ).encode("utf-8")
        headers = [(b"content-type", b"application/problem+json"), *cors_headers]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def _b64_digest(hasher: "hashlib._Hash") -> str:
    return base64.b64encode(hasher.digest()).decode("ascii")


def _write_sidecar(path: Path, *, content_type: str, checksum_sha256_b64: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"content_type": content_type, "checksum_sha256_b64": checksum_sha256_b64}, handle)
    os.replace(tmp, path)


def build_app() -> LocalBlobTransferApp:
    """Construct the app from `app.config` (spec §7.4 process wiring).

    Imports `app.config` here, not at module level: `config.py` freezes its
    constants (`DATABASE_URL`, `DATA_DIR`, ...) from `os.environ` the first
    time it is imported in a process. Merely importing this ASGI app module
    (e.g. to construct it in a test, or to reuse its signature helpers) must
    not force that read -- and must never race a test harness that sets
    `MARKETAI_DATABASE_URL`/`MARKETAI_DATA_DIR` before `app.config` is first
    imported.
    """
    from app import config

    if not config.BLOB_STORE_LOCAL_SIGNING_SECRET:
        raise RuntimeError(
            "MARKETAI_BLOB_STORE_LOCAL_SIGNING_SECRET must be set to run LocalBlobTransferApp"
        )
    return LocalBlobTransferApp(
        root=config.BLOB_STORE_LOCAL_ROOT,
        signing_secret=config.BLOB_STORE_LOCAL_SIGNING_SECRET.encode("utf-8"),
        cors_origin=config.BLOB_STORE_LOCAL_CORS_ORIGIN,
    )


def main() -> None:
    assert_local_blob_transfer_boundary()
    import uvicorn

    from app import config

    uvicorn.run(
        build_app(),
        host=config.BLOB_STORE_LOCAL_HOST,
        port=config.BLOB_STORE_LOCAL_PORT,
        access_log=False,
    )


if __name__ == "__main__":
    main()
