"""Merge the Creative Library and delegated BFF migration heads.

Revision ID: b9c0d1e2f3a4
Revises: a3b4c5d6e7f8, j1a2b3c4d5e6
Create Date: 2026-08-31 11:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence


revision: str = "b9c0d1e2f3a4"
down_revision: str | Sequence[str] | None = (
    "a3b4c5d6e7f8",
    "j1a2b3c4d5e6",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the two independently implemented migration branches."""


def downgrade() -> None:
    """Split the graph back into its two parent heads."""
