"""add transmetriq, sonar, planet_labs to telemetry_source enum

Revision ID: d4e8b2f6c0a9
Revises: c9d2a5f7e3b1
Create Date: 2026-10-26 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "d4e8b2f6c0a9"
down_revision: Union[str, None] = "c9d2a5f7e3b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Three new telemetry sources, each confirmed real via web search
    # before being added (see app/models/enums.py's TelemetrySource for
    # the confirmed-pricing notes on each): TransmetriQ (the real
    # car-level rail answer, replacing the "Railinc needs a shipper
    # relationship" dead end), FreightWaves SONAR (trucking macro
    # context), and Planet Labs (daily-revisit optical land-use change).
    # Same ADD VALUE requirement as every other new value added to this
    # shared enum (see 184d67a952e4, b094eff02892, a3f08e6c1d92,
    # c9d2a5f7e3b1).
    for value in ("transmetriq", "sonar", "planet_labs"):
        op.execute(f"ALTER TYPE telemetry_source ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no DROP VALUE -- same no-op downgrade as every other
    # ADD VALUE migration in this project.
    pass
