"""add webhook_calendar to activity_source enum

Revision ID: c3e5a7b9d024
Revises: b2d4f6a8e713
Create Date: 2026-10-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3e5a7b9d024'
down_revision: Union[str, None] = 'b2d4f6a8e713'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE activity_source ADD VALUE IF NOT EXISTS 'webhook_calendar'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE. Any row already written
    # with this source would need remapping before the type-swap technique
    # (see migration f6b06dccc093) could safely run; left as a no-op since
    # downgrading past this revision isn't expected in practice.
    pass
