"""add mca_history_batch_silo, mca_history_batch_upload enum values

Revision ID: d1f4a8c2e9b6
Revises: c9e3a5f7d8b2
Create Date: 2026-10-28 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "d1f4a8c2e9b6"
down_revision: Union[str, None] = "c9e3a5f7d8b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Second purchased-batch silo -- see app/models/enums.py's
    # PURCHASED_BATCH_SILOS and SiloName.MCA_HISTORY_BATCH_SILO
    # docstrings, and app/services/mca_history_batch.py.
    op.execute("ALTER TYPE silo_name ADD VALUE IF NOT EXISTS 'MCA History Batch Silo'")
    op.execute("ALTER TYPE telemetry_source ADD VALUE IF NOT EXISTS 'mca_history_batch_upload'")


def downgrade() -> None:
    # Postgres has no DROP VALUE -- same no-op downgrade as every other
    # ADD VALUE migration in this project.
    pass
