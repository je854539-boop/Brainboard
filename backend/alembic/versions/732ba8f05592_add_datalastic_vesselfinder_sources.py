"""add datalastic and vesselfinder to telemetry_source enum

Revision ID: 732ba8f05592
Revises: 2fabbbfa103a
Create Date: 2026-08-13 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "732ba8f05592"
down_revision: Union[str, None] = "2fabbbfa103a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Two new AIS vessel-tracking providers (Datalastic, VesselFinder),
    # wired for globe signals, per-lead enrichment, and macro telemetry
    # alike -- same ADD VALUE requirement as every other new source added
    # to this shared enum (see 184d67a952e4).
    for new_value in ("datalastic", "vesselfinder"):
        op.execute(f"ALTER TYPE telemetry_source ADD VALUE IF NOT EXISTS '{new_value}'")


def downgrade() -> None:
    # Postgres has no DROP VALUE; downgrading this enum addition in place
    # isn't supported (same as every other ADD VALUE migration in this
    # project -- see 184d67a952e4, which is likewise a no-op downgrade).
    pass
