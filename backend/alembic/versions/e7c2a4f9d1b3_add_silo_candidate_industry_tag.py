"""add industry_tag to silo_candidates

Revision ID: e7c2a4f9d1b3
Revises: d1f4a8c2e9b6
Create Date: 2026-10-29 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e7c2a4f9d1b3"
down_revision: Union[str, None] = "d1f4a8c2e9b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Free text, not an enum -- see app/models/orm.py's SiloCandidate.
    # industry_tag docstring. NULL on every existing candidate; backfilled
    # only on the next re-import, not retroactively guessed here.
    op.add_column("silo_candidates", sa.Column("industry_tag", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("silo_candidates", "industry_tag")
