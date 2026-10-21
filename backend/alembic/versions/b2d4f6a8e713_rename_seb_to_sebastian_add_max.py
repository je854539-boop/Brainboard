"""rename seb to sebastian, add max to co_broker roster

Revision ID: b2d4f6a8e713
Revises: a7b3e9f2c105
Create Date: 2026-10-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2d4f6a8e713'
down_revision: Union[str, None] = 'a7b3e9f2c105'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Simple in-place rename -- preserves every existing row's value.
    op.execute("ALTER TYPE co_broker RENAME VALUE 'Seb' TO 'Sebastian'")
    # Pure addition, no data remap needed (unlike the Jorge->Boula/Ariel
    # swap, nothing is being removed here).
    op.execute("ALTER TYPE co_broker ADD VALUE IF NOT EXISTS 'Max'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE -- removing 'Max' means
    # the same whole-type-swap technique used for the Jorge removal.
    # Any existing 'Max' row has no natural inverse value, so it's left
    # as an error case (there shouldn't be one on a downgrade from this
    # revision, since 'Max' was only just added here).
    op.execute("ALTER TYPE co_broker RENAME VALUE 'Sebastian' TO 'Seb'")
    op.execute("ALTER TYPE co_broker RENAME TO co_broker_new")
    op.execute(
        "CREATE TYPE co_broker AS ENUM ("
        "'Nick F','Mike F','Vinny','Victor','Gallo','Shaun','Kris','DanStol','Roman','James',"
        "'Alfred','Marcus','Ricky','Zack','Emilio','Boula','Ariel','Tony','Seb')"
    )
    op.execute("ALTER TABLE master_log_entries ALTER COLUMN co_broker TYPE co_broker USING co_broker::text::co_broker")
    op.execute("ALTER TABLE inbound_ring_targets ALTER COLUMN co_broker TYPE co_broker USING co_broker::text::co_broker")
    op.execute("ALTER TABLE lead_contributors ALTER COLUMN co_broker TYPE co_broker USING co_broker::text::co_broker")
    op.execute(
        "ALTER TABLE dialer_call_attempts ALTER COLUMN answered_by_co_broker TYPE co_broker "
        "USING answered_by_co_broker::text::co_broker"
    )
    op.execute("DROP TYPE co_broker_new")
