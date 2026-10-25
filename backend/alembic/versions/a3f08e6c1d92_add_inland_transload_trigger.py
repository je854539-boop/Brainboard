"""add inland_transload_confirmed to waterway_trigger_type enum

Revision ID: a3f08e6c1d92
Revises: e4b9c7a1f358
Create Date: 2026-10-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "a3f08e6c1d92"
down_revision: Union[str, None] = "e4b9c7a1f358"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Third river_surveillance.py trigger: a company's cargo resolved at
    # a coastal/anchorage zone and again at a river/lock zone within the
    # transload window -- real confirmation of an ocean-to-barge handoff,
    # not two unrelated sightings. See check_inland_transload for why this
    # matches on resolved company identity rather than vessel identity
    # (a transload swaps the vessel entirely). Same ADD VALUE requirement
    # as every other new value added to a shared enum in this project
    # (see 184d67a952e4, b094eff02892).
    op.execute("ALTER TYPE waterway_trigger_type ADD VALUE IF NOT EXISTS 'inland_transload_confirmed'")


def downgrade() -> None:
    # Postgres has no DROP VALUE -- same no-op downgrade as every other
    # ADD VALUE migration in this project.
    pass
