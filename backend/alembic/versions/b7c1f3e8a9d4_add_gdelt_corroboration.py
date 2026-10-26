"""add persisted_macro_signals table and gdelt corroboration columns on waterway_triggers

Revision ID: b7c1f3e8a9d4
Revises: a3f08e6c1d92
Create Date: 2026-10-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7c1f3e8a9d4"
down_revision: Union[str, None] = "a3f08e6c1d92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PersistedMacroSignal -- durable row for a classified GDELT-origin
    # MacroSignal (silo_leadgen.py). The raw headline already has a
    # durable row (TelemetryEvent, source='gdelt'); what wasn't durable
    # before this migration was the INTERPRETATION a sweep derives from
    # it (commodity tag, direction, US state if one was extracted) --
    # that lived only in memory for the duration of one run_silo_leadgen
    # call. See PersistedMacroSignal's docstring in app/models/orm.py.
    op.create_table(
        "persisted_macro_signals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_reference", sa.String(length=512), nullable=True),
        sa.Column("commodity", sa.String(length=128), nullable=True),
        sa.Column("signal_type", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_persisted_macro_signals_kind"), "persisted_macro_signals", ["kind"], unique=False)
    op.create_index(op.f("ix_persisted_macro_signals_commodity"), "persisted_macro_signals", ["commodity"], unique=False)
    op.create_index(op.f("ix_persisted_macro_signals_state"), "persisted_macro_signals", ["state"], unique=False)
    op.create_index(op.f("ix_persisted_macro_signals_created_at"), "persisted_macro_signals", ["created_at"], unique=False)

    # Annotation columns on waterway_triggers -- never a gate (every
    # existing trigger still fires on vessel/dock behavior alone). See
    # river_surveillance.py::_annotate_gdelt_corroboration.
    op.add_column(
        "waterway_triggers",
        sa.Column("gdelt_corroborated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("waterway_triggers", sa.Column("gdelt_context", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("waterway_triggers", "gdelt_context")
    op.drop_column("waterway_triggers", "gdelt_corroborated")

    op.drop_index(op.f("ix_persisted_macro_signals_created_at"), table_name="persisted_macro_signals")
    op.drop_index(op.f("ix_persisted_macro_signals_state"), table_name="persisted_macro_signals")
    op.drop_index(op.f("ix_persisted_macro_signals_commodity"), table_name="persisted_macro_signals")
    op.drop_index(op.f("ix_persisted_macro_signals_kind"), table_name="persisted_macro_signals")
    op.drop_table("persisted_macro_signals")
