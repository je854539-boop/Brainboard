"""add xeneta to telemetry_source enum

Revision ID: e5f9c3a7d1b2
Revises: d4e8b2f6c0a9
Create Date: 2026-10-26 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "e5f9c3a7d1b2"
down_revision: Union[str, None] = "d4e8b2f6c0a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Xeneta -- confirmed real air-freight rate/capacity intelligence
    # (see app/models/enums.py's TelemetrySource.XENETA), the missing
    # rate/capacity complement to ADS-B's position tracking. Same ADD
    # VALUE requirement as every other new value added to this shared
    # enum (see 184d67a952e4, b094eff02892, a3f08e6c1d92, c9d2a5f7e3b1,
    # d4e8b2f6c0a9).
    op.execute("ALTER TYPE telemetry_source ADD VALUE IF NOT EXISTS 'xeneta'")


def downgrade() -> None:
    # Postgres has no DROP VALUE -- same no-op downgrade as every other
    # ADD VALUE migration in this project.
    pass
