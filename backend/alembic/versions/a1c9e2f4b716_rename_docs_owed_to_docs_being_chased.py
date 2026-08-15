"""rename Docs Owed status to Docs Being Chased

Revision ID: a1c9e2f4b716
Revises: f6b06dccc093
Create Date: 2026-08-15 21:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c9e2f4b716'
down_revision: Union[str, None] = 'f6b06dccc093'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE master_log_status RENAME VALUE 'Docs Owed' TO 'Docs Being Chased'")


def downgrade() -> None:
    op.execute("ALTER TYPE master_log_status RENAME VALUE 'Docs Being Chased' TO 'Docs Owed'")
