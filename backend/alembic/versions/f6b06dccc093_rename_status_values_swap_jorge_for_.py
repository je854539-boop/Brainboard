"""rename status values, swap jorge for boula and ariel

Revision ID: f6b06dccc093
Revises: c47451583649
Create Date: 2026-08-15 20:10:28.852744

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6b06dccc093'
down_revision: Union[str, None] = 'c47451583649'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Simple in-place renames -- ALTER TYPE ... RENAME VALUE preserves
    # every existing row's value automatically, no data migration needed.
    op.execute("ALTER TYPE master_log_status RENAME VALUE 'In negotiation' TO 'In Negotiation'")
    op.execute("ALTER TYPE master_log_status RENAME VALUE 'Loss to Competitor' TO 'Lost To Competitor'")

    # co_broker: Postgres has no ALTER TYPE ... DROP VALUE, so replacing
    # Jorge with Boula/Ariel means swapping in a whole new type. Any
    # existing 'Jorge' row is remapped to 'Boula' as part of the column
    # type change so the swap can't fail on data still referencing the
    # value being removed.
    op.execute("ALTER TYPE co_broker RENAME TO co_broker_old")
    op.execute(
        "CREATE TYPE co_broker AS ENUM ("
        "'Nick F','Mike F','Vinny','Victor','Gallo','Shaun','Kris','DanStol','Roman','James',"
        "'Alfred','Marcus','Ricky','Zack','Emilio','Boula','Ariel','Tony','Seb')"
    )
    op.execute(
        "ALTER TABLE master_log_entries ALTER COLUMN co_broker TYPE co_broker USING "
        "(CASE co_broker::text WHEN 'Jorge' THEN 'Boula' ELSE co_broker::text END)::co_broker"
    )
    op.execute("DROP TYPE co_broker_old")


def downgrade() -> None:
    op.execute("ALTER TYPE master_log_status RENAME VALUE 'In Negotiation' TO 'In negotiation'")
    op.execute("ALTER TYPE master_log_status RENAME VALUE 'Lost To Competitor' TO 'Loss to Competitor'")

    # Lossy: both Boula and Ariel collapse back to Jorge (there's no
    # natural inverse for the 1-value-becomes-2 direction).
    op.execute("ALTER TYPE co_broker RENAME TO co_broker_new")
    op.execute(
        "CREATE TYPE co_broker AS ENUM ("
        "'Nick F','Mike F','Vinny','Victor','Gallo','Shaun','Kris','DanStol','Roman','James',"
        "'Alfred','Marcus','Ricky','Zack','Emilio','Jorge','Tony','Seb')"
    )
    op.execute(
        "ALTER TABLE master_log_entries ALTER COLUMN co_broker TYPE co_broker USING "
        "(CASE co_broker::text WHEN 'Boula' THEN 'Jorge' WHEN 'Ariel' THEN 'Jorge' ELSE co_broker::text END)::co_broker"
    )
    op.execute("DROP TYPE co_broker_new")
