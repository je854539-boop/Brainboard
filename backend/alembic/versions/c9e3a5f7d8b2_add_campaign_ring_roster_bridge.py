"""add bridge_to_ring_roster to dialer_campaigns

Revision ID: c9e3a5f7d8b2
Revises: b7d2f4a9c6e1
Create Date: 2026-10-28 11:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c9e3a5f7d8b2"
down_revision: Union[str, None] = "b7d2f4a9c6e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # See app/models/orm.py's DialerCampaign.bridge_to_ring_roster
    # docstring. server_default 'false' so every existing campaign keeps
    # its exact current single-number bridge behavior with zero migration
    # risk.
    op.add_column(
        "dialer_campaigns",
        sa.Column("bridge_to_ring_roster", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("dialer_campaigns", "bridge_to_ring_roster")
