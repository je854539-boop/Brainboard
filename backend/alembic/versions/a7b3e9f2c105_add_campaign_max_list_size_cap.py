"""add max_list_size compliance cap to dialer_campaigns

Revision ID: a7b3e9f2c105
Revises: f4a7c1e93b52
Create Date: 2026-10-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7b3e9f2c105'
down_revision: Union[str, None] = 'f4a7c1e93b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'dialer_campaigns',
        sa.Column('max_list_size', sa.Integer(), nullable=False, server_default='1000'),
    )


def downgrade() -> None:
    op.drop_column('dialer_campaigns', 'max_list_size')
