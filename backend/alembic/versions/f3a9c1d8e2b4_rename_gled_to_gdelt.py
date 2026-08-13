"""rename gled to gdelt in telemetry_source enum

Revision ID: f3a9c1d8e2b4
Revises: 184d67a952e4
Create Date: 2026-08-13 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "f3a9c1d8e2b4"
down_revision: Union[str, None] = "184d67a952e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # "GLED" was a placeholder codename for an unconfirmed data provider;
    # the user confirmed it's GDELT. Postgres supports a clean in-place
    # rename of an enum value (10+), so no data migration is needed --
    # existing rows with source='gled' become source='gdelt' automatically.
    op.execute("ALTER TYPE telemetry_source RENAME VALUE 'gled' TO 'gdelt'")


def downgrade() -> None:
    op.execute("ALTER TYPE telemetry_source RENAME VALUE 'gdelt' TO 'gled'")
