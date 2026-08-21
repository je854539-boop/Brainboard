"""inbound ring group and contributor credit ledger

Revision ID: f4a7c1e93b52
Revises: d3f8a2b6c910
Create Date: 2026-08-21 15:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'f4a7c1e93b52'
down_revision: Union[str, None] = 'd3f8a2b6c910'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE TYPE dialer_call_direction AS ENUM ('outbound', 'inbound')")
    op.execute("CREATE TYPE lead_contribution_reason AS ENUM ('inbound_call_answered')")

    op.add_column(
        'dialer_call_attempts',
        sa.Column(
            'direction', sa.Enum(name='dialer_call_direction', create_type=False),
            nullable=False, server_default='outbound',
        ),
    )
    op.add_column(
        'dialer_call_attempts',
        sa.Column('answered_by_co_broker', sa.Enum(name='co_broker', create_type=False), nullable=True),
    )
    # campaign_id: outbound attempts always have one, inbound never does --
    # was NOT NULL, has to become nullable for inbound rows to exist at all.
    op.alter_column('dialer_call_attempts', 'campaign_id', nullable=True)

    # postgresql.ENUM(..., create_type=False) rather than the generic
    # sa.Enum -- inside op.create_table specifically, sa.Enum's
    # create_type flag isn't reliably honored (it still fired a
    # `CREATE TYPE co_broker AS ENUM ()` DDL event on table creation the
    # first time this was written, even with create_type=False set,
    # despite add_column above correctly respecting it). The postgres-
    # dialect ENUM class does not have that gap.
    co_broker_type = postgresql.ENUM(name='co_broker', create_type=False)
    reason_type = postgresql.ENUM(name='lead_contribution_reason', create_type=False)

    op.create_table(
        'inbound_ring_targets',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('co_broker', co_broker_type, nullable=False),
        sa.Column('phone_number', sa.String(32), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'lead_contributors',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'lead_uid', postgresql.UUID(as_uuid=True),
            sa.ForeignKey('master_log_entries.lead_uid', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('co_broker', co_broker_type, nullable=False),
        sa.Column('reason', reason_type, nullable=False),
        sa.Column('credited_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_lead_contributors_lead_uid', 'lead_contributors', ['lead_uid'])


def downgrade() -> None:
    op.drop_index('ix_lead_contributors_lead_uid', table_name='lead_contributors')
    op.drop_table('lead_contributors')
    op.drop_table('inbound_ring_targets')
    op.alter_column('dialer_call_attempts', 'campaign_id', nullable=False)
    op.drop_column('dialer_call_attempts', 'answered_by_co_broker')
    op.drop_column('dialer_call_attempts', 'direction')
    op.execute("DROP TYPE lead_contribution_reason")
    op.execute("DROP TYPE dialer_call_direction")
