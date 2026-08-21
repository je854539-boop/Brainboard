"""track silo origin on leads and candidates

Revision ID: d3f8a2b6c910
Revises: a1c9e2f4b716
Create Date: 2026-08-21 15:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3f8a2b6c910'
down_revision: Union[str, None] = 'a1c9e2f4b716'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Reuse the existing silo_name/telemetry_source enum types -- no new
    # types needed, these columns just aren't part of the Master Log V2
    # A-T sheet mapping (same precedent as latitude/longitude on
    # silo_candidates).
    op.add_column(
        'master_log_entries',
        sa.Column('source_silo', sa.Enum(name='silo_name', create_type=False), nullable=True),
    )
    op.add_column(
        'master_log_entries',
        sa.Column('source_channel', sa.Enum(name='telemetry_source', create_type=False), nullable=True),
    )
    op.add_column(
        'silo_candidates',
        sa.Column('origin_source', sa.Enum(name='telemetry_source', create_type=False), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('silo_candidates', 'origin_source')
    op.drop_column('master_log_entries', 'source_channel')
    op.drop_column('master_log_entries', 'source_silo')
