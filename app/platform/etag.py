"""ETag / If-Match revision contract (spec §2).

Mutable aggregates expose a monotonically increasing ``revision``. Mutating
commands must present it via ``If-Match`` so a stale write is rejected
(409) instead of silently clobbering a concurrent change; a missing header
is rejected up front (428) rather than allowed to race.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, Response

__all__ = ["extract_if_match", "check_revision_match", "set_etag"]


def extract_if_match(request: Request) -> int:
    """Parse the ``If-Match: "<revision>"`` header into an integer revision.

    Raises ``HTTPException(428)`` if the header is missing and
    ``HTTPException(400)`` if it cannot be parsed as an integer.
    """
    raw = request.headers.get("if-match")
    if raw is None:
        raise HTTPException(
            status_code=428,
            detail={
                "code": "precondition_required",
                "message": "If-Match header is required",
            },
        )
    cleaned = raw.strip().strip('"')
    try:
        return int(cleaned)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_precondition",
                "message": f"If-Match must be a quoted integer, got: {raw}",
            },
        )


def check_revision_match(current_revision: int, expected_revision: int) -> None:
    """Raise ``HTTPException(409, "revision_conflict")`` if ``current_revision``
    does not match the caller's ``expected_revision``."""
    if current_revision != expected_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "revision_conflict",
                "message": (
                    f"Expected revision {expected_revision}, "
                    f"current is {current_revision}"
                ),
                "current_revision": current_revision,
                "expected_revision": expected_revision,
            },
        )


def set_etag(response: Response, revision: int) -> None:
    """Set the response's ``ETag`` header from an aggregate's ``revision``."""
    response.headers["ETag"] = f'"{revision}"'
