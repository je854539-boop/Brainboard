"""add usace to telemetry_source enum

Revision ID: b094eff02892
Revises: 732ba8f05592
Create Date: 2026-08-13 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "b094eff02892"
down_revision: Union[str, None] = "732ba8f05592"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # USACE lock-status/closure feed, wired for macro telemetry sweeps
    # feeding the CME Macro Funnel silo (barge-traffic disruption on the
    # Mississippi/Illinois river system is a leading indicator for Chicago
    # commodities-market volatility) -- same ADD VALUE requirement as
    # every other new source added to this shared enum (see 184d67a952e4).
    op.execute("ALTER TYPE telemetry_source ADD VALUE IF NOT EXISTS 'usace'")


def downgrade() -> None:
    # Postgres has no DROP VALUE -- same no-op downgrade as every other
    # ADD VALUE migration in this project (see 184d67a952e4).
    pass
