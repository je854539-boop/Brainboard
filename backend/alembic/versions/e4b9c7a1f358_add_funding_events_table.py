"""add funding_events table (full repeat-funding history)

Revision ID: e4b9c7a1f358
Revises: d8e1f4a9c632
Create Date: 2026-10-21 00:00:00.000002

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e4b9c7a1f358'
down_revision: Union[str, None] = 'd8e1f4a9c632'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

date_precision_enum = postgresql.ENUM("exact", "month", name="date_precision", create_type=False)


def upgrade() -> None:
    op.create_table(
        "funding_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lead_uid", postgresql.UUID(as_uuid=True), sa.ForeignKey("master_log_entries.lead_uid", ondelete="CASCADE"), nullable=False),
        sa.Column("funded_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("funded_date", sa.Date(), nullable=True),
        sa.Column("funded_date_precision", date_precision_enum, nullable=True),
        sa.Column("term_weeks", sa.Numeric(6, 1), nullable=True),
        sa.Column("factor_rate", sa.Numeric(5, 3), nullable=True),
        sa.Column("payment_amt", sa.Numeric(14, 2), nullable=True),
        sa.Column("payment_freq", sa.String(64), nullable=True),
        sa.Column("lender", sa.String(256), nullable=True),
        sa.Column("position_number", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_funding_events_lead_uid", "funding_events", ["lead_uid"])


def downgrade() -> None:
    op.drop_index("ix_funding_events_lead_uid", table_name="funding_events")
    op.drop_table("funding_events")
