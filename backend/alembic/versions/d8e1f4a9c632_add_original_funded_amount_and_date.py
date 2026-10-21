"""add original_funded_amount, funded_date, funded_date_precision

Revision ID: d8e1f4a9c632
Revises: c3e5a7b9d024
Create Date: 2026-10-21 00:00:00.000001

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd8e1f4a9c632'
down_revision: Union[str, None] = 'c3e5a7b9d024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

date_precision_enum = postgresql.ENUM("exact", "month", name="date_precision", create_type=False)


def upgrade() -> None:
    date_precision_enum.create(op.get_bind(), checkfirst=True)
    op.add_column("master_log_entries", sa.Column("original_funded_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column("master_log_entries", sa.Column("funded_date", sa.Date(), nullable=True))
    op.add_column(
        "master_log_entries",
        sa.Column("funded_date_precision", date_precision_enum, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("master_log_entries", "funded_date_precision")
    op.drop_column("master_log_entries", "funded_date")
    op.drop_column("master_log_entries", "original_funded_amount")
    date_precision_enum.drop(op.get_bind(), checkfirst=True)
