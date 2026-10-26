"""add sentinel_hub to telemetry_source enum

Revision ID: f1a6d8e2b4c7
Revises: e5f9c3a7d1b2
Create Date: 2026-10-26 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "f1a6d8e2b4c7"
down_revision: Union[str, None] = "e5f9c3a7d1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Sentinel Hub -- real commercial-friendly access to Sentinel-1 SAR
    # (see app/models/enums.py's TelemetrySource.SENTINEL_HUB). Serves
    # both disaster-damage confirmation (silo_leadgen.py) and the real
    # AIS coverage gap on inland barges (river_surveillance.py). Same
    # ADD VALUE requirement as every other new value added to this
    # shared enum.
    op.execute("ALTER TYPE telemetry_source ADD VALUE IF NOT EXISTS 'sentinel_hub'")


def downgrade() -> None:
    # Postgres has no DROP VALUE -- same no-op downgrade as every other
    # ADD VALUE migration in this project.
    pass
