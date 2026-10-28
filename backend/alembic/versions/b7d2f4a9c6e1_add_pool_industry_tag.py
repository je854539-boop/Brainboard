"""add industry_tag to dialer_number_pools

Revision ID: b7d2f4a9c6e1
Revises: a2c4e6f8b1d3
Create Date: 2026-10-28 10:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7d2f4a9c6e1"
down_revision: Union[str, None] = "a2c4e6f8b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Free text, not an enum -- see app/models/orm.py's DialerNumberPool.
    # industry_tag docstring. NULL on every existing pool, so inbound
    # routing keeps its exact current behavior until a pool opts in.
    op.add_column("dialer_number_pools", sa.Column("industry_tag", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("dialer_number_pools", "industry_tag")
