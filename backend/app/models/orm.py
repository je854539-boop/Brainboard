import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base
from app.models.enums import CoBroker, MasterLogStatus, SiloCandidateStatus, SiloName, TelemetrySource


def _pg_enum(enum_cls, name: str) -> SAEnum:
    """SQLAlchemy's Enum type persists the Python member *name* by default
    (e.g. "NICK_F"). Our enum values are the canonical validated strings
    (e.g. "Nick F") that must match the Google Sheet string-for-string, so
    every enum column must be declared through this helper instead of
    calling SAEnum(...) directly."""
    return SAEnum(enum_cls, name=name, values_callable=lambda e: [member.value for member in e])


class MasterLogEntry(Base):
    """Mirrors Master Log V2 (columns A-X).

    Named columns below map to the fields with dedicated sync behavior:
      Col K -> follow_up_date  (onMasterLogEdit pushes this to Calendar)
      Col L -> notes           (onMasterLogEdit pushes this to Calendar description)
      Col X -> dossier_drive_link (Macro Dossier Drive Link)
    The remaining Master Log V2 columns are preserved verbatim in
    `extra_columns` (JSONB) so the sheet's full 24-column shape round-trips
    through sync without lossy remapping.
    """

    __tablename__ = "master_log_entries"

    lead_uid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    business_name: Mapped[str] = mapped_column(String(256), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(256))
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(256))

    co_broker: Mapped[CoBroker] = mapped_column(_pg_enum(CoBroker, "co_broker"), nullable=False)
    status: Mapped[MasterLogStatus] = mapped_column(
        _pg_enum(MasterLogStatus, "master_log_status"), nullable=False, default=MasterLogStatus.NEW_LEAD
    )

    loan_amount_requested: Mapped[float | None] = mapped_column(Numeric(14, 2))

    follow_up_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # Column K
    notes: Mapped[str | None] = mapped_column(Text)  # Column L
    dossier_drive_link: Mapped[str | None] = mapped_column(String(1024))  # Column X

    calendar_event_id: Mapped[str | None] = mapped_column(String(256))
    extra_columns: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    status_history: Mapped[list["StatusHistory"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )
    hazard_snapshot: Mapped["HazardSnapshot"] = relationship(
        back_populates="lead", uselist=False, cascade="all, delete-orphan"
    )


class StatusHistory(Base):
    """Append-only log of pipeline status transitions per lead. Backs both
    the Kaplan-Meier survival curve (time-to-Funded / time-to-attrition)
    and the empirical Markov stage-to-stage transition matrix."""

    __tablename__ = "status_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_uid: Mapped[uuid.UUID] = mapped_column(ForeignKey("master_log_entries.lead_uid", ondelete="CASCADE"))
    from_status: Mapped[MasterLogStatus | None] = mapped_column(_pg_enum(MasterLogStatus, "master_log_status"))
    to_status: Mapped[MasterLogStatus] = mapped_column(_pg_enum(MasterLogStatus, "master_log_status"))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lead: Mapped["MasterLogEntry"] = relationship(back_populates="status_history")


class SiloCandidate(Base):
    """Mirrors the 8-column macro silo tab schema.

      Col A -> company_name
      Col B -> contact_name
      Col C -> phone
      Col D -> source_reference   (e.g. filing/manifest/parcel reference)
      Col E -> notes
      Col F -> dossier_drive_link
      Col G -> score
      Col H -> status (pending / converted / dismissed)
    """

    __tablename__ = "silo_candidates"

    candidate_uid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    silo: Mapped[SiloName] = mapped_column(_pg_enum(SiloName, "silo_name"), nullable=False)

    company_name: Mapped[str] = mapped_column(String(256), nullable=False)  # Col A
    contact_name: Mapped[str | None] = mapped_column(String(256))  # Col B
    phone: Mapped[str | None] = mapped_column(String(64))  # Col C
    source_reference: Mapped[str | None] = mapped_column(String(512))  # Col D
    notes: Mapped[str | None] = mapped_column(Text)  # Col E
    dossier_drive_link: Mapped[str | None] = mapped_column(String(1024))  # Col F
    score: Mapped[float | None] = mapped_column(Numeric(6, 2))  # Col G
    status: Mapped[SiloCandidateStatus] = mapped_column(
        _pg_enum(SiloCandidateStatus, "silo_candidate_status"),
        nullable=False,
        default=SiloCandidateStatus.PENDING,
    )  # Col H

    converted_lead_uid: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("master_log_entries.lead_uid", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TelemetryEvent(Base):
    """Raw macro-surveillance ingestion record from an external data
    provider, optionally linked to a lead/candidate UUID and embedded via
    pgvector for semantic dossier correlation."""

    __tablename__ = "telemetry_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[TelemetrySource] = mapped_column(_pg_enum(TelemetrySource, "telemetry_source"), nullable=False)
    entity_uid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HazardSnapshot(Base):
    """Cached latest hazard-engine output for a lead, refreshed whenever
    the Kaplan-Meier / Markov engine recomputes."""

    __tablename__ = "hazard_snapshots"

    lead_uid: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("master_log_entries.lead_uid", ondelete="CASCADE"), primary_key=True
    )
    survival_probability: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    days_in_stage: Mapped[int] = mapped_column(nullable=False, default=0)
    next_stage_transition_probability: Mapped[float | None] = mapped_column(Numeric(5, 4))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    lead: Mapped["MasterLogEntry"] = relationship(back_populates="hazard_snapshot")
