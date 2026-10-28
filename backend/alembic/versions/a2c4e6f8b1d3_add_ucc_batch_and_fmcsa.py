"""add ucc_batch_silo, fmcsa, ucc_batch_upload enum values

Revision ID: a2c4e6f8b1d3
Revises: f1a6d8e2b4c7
Create Date: 2026-10-28 09:40:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "a2c4e6f8b1d3"
down_revision: Union[str, None] = "f1a6d8e2b4c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # UCC Batch Silo -- a 10th silo tab for the one-time bulk UCC-lien
    # batch, kept separate from the other 9 macro-surveillance silos (its
    # own spreadsheet, its own shadow-mode population). See
    # app/models/enums.py's SiloName.UCC_BATCH_SILO and
    # app/services/ucc_batch.py.
    op.execute("ALTER TYPE silo_name ADD VALUE IF NOT EXISTS 'UCC Batch Silo'")
    # FMCSA Company Snapshot / QCMobile -- real, confirmed-free fleet-size
    # enrichment adapter. See app/services/enrichment/fmcsa.py.
    op.execute("ALTER TYPE telemetry_source ADD VALUE IF NOT EXISTS 'fmcsa'")
    # Tag for SiloCandidate rows created from the one-time UCC batch CSV
    # import, distinct from the live (inert) per-state UCC_FILINGS pull.
    op.execute("ALTER TYPE telemetry_source ADD VALUE IF NOT EXISTS 'ucc_batch_upload'")


def downgrade() -> None:
    # Postgres has no DROP VALUE -- same no-op downgrade as every other
    # ADD VALUE migration in this project.
    pass
