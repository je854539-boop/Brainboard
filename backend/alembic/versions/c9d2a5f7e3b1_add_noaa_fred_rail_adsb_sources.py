"""add noaa_nws, fred, rail_traffic, adsb to telemetry_source enum

Revision ID: c9d2a5f7e3b1
Revises: b7c1f3e8a9d4
Create Date: 2026-10-26 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "c9d2a5f7e3b1"
down_revision: Union[str, None] = "b7c1f3e8a9d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Four new telemetry sources (see app/models/enums.py's TelemetrySource
    # for the honesty notes on each): NOAA NWS structured disaster alerts
    # (real, free, keyless), FRED credit-tightening (real, free w/ signup
    # key), and rail_traffic/adsb (real provider categories, but their
    # adapters' fetch() calls are honestly-flagged placeholders until a
    # confirmed live endpoint replaces them). Same ADD VALUE requirement
    # as every other new value added to this shared enum (see 184d67a952e4,
    # b094eff02892, a3f08e6c1d92).
    for value in ("noaa_nws", "fred", "rail_traffic", "adsb"):
        op.execute(f"ALTER TYPE telemetry_source ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no DROP VALUE -- same no-op downgrade as every other
    # ADD VALUE migration in this project.
    pass
