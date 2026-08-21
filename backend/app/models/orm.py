import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base
from app.models.enums import (
    ActivityEventType,
    ActivitySource,
    BrainMode,
    CallAnalysisStatus,
    CoBroker,
    DialerCallDirection,
    DialerCallStatus,
    DialerCampaignType,
    DialerDisposition,
    GeofenceEventType,
    LeadContributionReason,
    MasterLogStatus,
    SiloCandidateStatus,
    SiloName,
    TelemetrySource,
    WaterwayTriggerType,
)


def _pg_enum(enum_cls, name: str) -> SAEnum:
    """SQLAlchemy's Enum type persists the Python member *name* by default
    (e.g. "NICK_F"). Our enum values are the canonical validated strings
    (e.g. "Nick F") that must match the Google Sheet string-for-string, so
    every enum column must be declared through this helper instead of
    calling SAEnum(...) directly."""
    return SAEnum(enum_cls, name=name, values_callable=lambda e: [member.value for member in e])


class MasterLogEntry(Base):
    """Mirrors Master Log V2 (columns A-T -- see
    app/services/google/sheets_sync.py::MASTER_LOG_COLUMN_FIELDS for the
    exact column-to-field mapping, kept in lockstep with
    apps_script/Code.gs's ML_COL_* constants).

    follow_up_date and notes push to the linked Calendar event on edit
    (onMasterLogEdit); dossier_drive_link/financials_link/transcripts_link
    are the three per-lead document links the dashboard surfaces as
    clickable buttons. Any columns beyond the mapped set are preserved
    verbatim in `extra_columns` (JSONB) so the sheet round-trips without
    lossy remapping.
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

    # MCA intake fields (from the lead intake terminal)
    state: Mapped[str | None] = mapped_column(String(64))
    annual_revenue: Mapped[float | None] = mapped_column(Numeric(14, 2))
    lender: Mapped[str | None] = mapped_column(String(256))
    payment_amt: Mapped[float | None] = mapped_column(Numeric(14, 2))
    payment_freq: Mapped[str | None] = mapped_column(String(64))
    current_balance: Mapped[float | None] = mapped_column(Numeric(14, 2))
    open_positions: Mapped[int | None] = mapped_column()
    credit_score: Mapped[int | None] = mapped_column()

    follow_up_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    dossier_drive_link: Mapped[str | None] = mapped_column(String(1024))
    financials_link: Mapped[str | None] = mapped_column(String(1024))
    transcripts_link: Mapped[str | None] = mapped_column(String(1024))

    calendar_event_id: Mapped[str | None] = mapped_column(String(256))
    extra_columns: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Not sheet columns (the A-T mapping above is fixed) -- same "extra
    # structured field beyond the sheet mirror" precedent as
    # SiloCandidate.latitude/longitude. Set once, at conversion time, by
    # pipeline.convert_or_update_silo_candidate; NULL for a lead entered
    # directly (intake form, Sheet row typed by hand). This is what lets
    # Brain know a lead originated from silo lead-gen instead of manual
    # intake, and which silo/telemetry source specifically -- without
    # touching the Master Log V2 sheet layout at all.
    source_silo: Mapped[SiloName | None] = mapped_column(_pg_enum(SiloName, "silo_name"))
    source_channel: Mapped[TelemetrySource | None] = mapped_column(_pg_enum(TelemetrySource, "telemetry_source"))

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
    activity_events: Mapped[list["LeadActivityEvent"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
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
    # Not a sheet column (the 8-column silo tab schema above is fixed) --
    # same "extra structured field beyond the sheet mirror" precedent as
    # latitude/longitude below. Populated by the CME Macro Funnel
    # waterfall's stage 4 contact enrichment, see silo_leadgen.py.
    email: Mapped[str | None] = mapped_column(String(256))
    score: Mapped[float | None] = mapped_column(Numeric(6, 2))  # Col G
    status: Mapped[SiloCandidateStatus] = mapped_column(
        _pg_enum(SiloCandidateStatus, "silo_candidate_status"),
        nullable=False,
        default=SiloCandidateStatus.PENDING,
    )  # Col H

    converted_lead_uid: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("master_log_entries.lead_uid", ondelete="SET NULL")
    )

    # Not a sheet column -- same precedent as email/latitude/longitude.
    # The specific telemetry provider that identified this candidate,
    # parsed once at creation time from source_reference's existing
    # "[provider] ..." tag convention (see
    # silo_leadgen.py::_infer_origin_source) into a real, queryable
    # column instead of leaving conversion-rate-by-source analytics
    # dependent on parsing free text every time. NULL only if a
    # candidate's source_reference somehow doesn't carry a recognized
    # tag -- shouldn't happen given every creation site sets one, but
    # not force-defaulted to a guess if it does.
    origin_source: Mapped[TelemetrySource | None] = mapped_column(_pg_enum(TelemetrySource, "telemetry_source"))

    # Nullable, populated only when the identifying telemetry actually
    # carried real coordinates (currently: Regrid parcel GeoJSON geometry,
    # see silo_leadgen.py::_identify_via_regrid) -- most silos have no
    # inherent geo signal in their source data, and those candidates
    # simply have no position rather than a guessed one. Powers the
    # globe's silo-funnel-lifecycle layer (creation -> conversion ->
    # funded), see app/routers/silo.py::candidates_geo.
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))

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


class LeadActivityEvent(Base):
    """Append-only ledger of every lead-lifecycle event -- note edits,
    follow-up/Column K changes, status transitions, Calendar syncs, UI
    clicks, enrichment runs, inbound Sheet edits. This is the covariate
    stream the time-varying hazard model (hazard_engine.fit_cox_time_varying)
    consumes; StatusHistory alone only captures stage transitions."""

    __tablename__ = "lead_activity_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_uid: Mapped[uuid.UUID] = mapped_column(ForeignKey("master_log_entries.lead_uid", ondelete="CASCADE"), index=True)
    event_type: Mapped[ActivityEventType] = mapped_column(_pg_enum(ActivityEventType, "activity_event_type"), nullable=False)
    source: Mapped[ActivitySource] = mapped_column(_pg_enum(ActivitySource, "activity_source"), nullable=False)
    field_name: Mapped[str | None] = mapped_column(String(128))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    lead: Mapped["MasterLogEntry"] = relationship(back_populates="activity_events")


class EnrichmentResult(Base):
    """Targeted per-lead/per-candidate enrichment lookup result (as
    opposed to TelemetryEvent's passive macro-silo sweeps). `entity_uid`
    matches a lead_uid or candidate_uid -- whichever was enriched."""

    __tablename__ = "enrichment_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_uid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    source: Mapped[TelemetrySource] = mapped_column(_pg_enum(TelemetrySource, "telemetry_source"), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShadowScore(Base):
    """A logged prediction from the Brain scoring engine. Every lead gets
    scored on every recompute regardless of mode; `mode` records whether
    the Brain was still in SHADOW (silent, pre-1000-lead) or LIVE
    (surfaced as a recommendation) at the time of that prediction, so
    shadow-era predictions can be back-tested once outcomes land."""

    __tablename__ = "shadow_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_uid: Mapped[uuid.UUID] = mapped_column(ForeignKey("master_log_entries.lead_uid", ondelete="CASCADE"), index=True)
    predicted_funded_probability: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[BrainMode] = mapped_column(_pg_enum(BrainMode, "brain_mode"), nullable=False)
    training_set_size: Mapped[int] = mapped_column(nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class GlobeSignal(Base):
    """A geolocated ping plotted on the 3D globe -- Global Fishing Watch
    4Wings vessel-activity tiles, GDELT conflict-zone events, etc."""

    __tablename__ = "globe_signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[TelemetrySource] = mapped_column(_pg_enum(TelemetrySource, "telemetry_source"), nullable=False)
    latitude: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    longitude: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    intensity: Mapped[float | None] = mapped_column(Numeric(10, 4))
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    entity_uid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class CallRecording(Base):
    """A post-call analysis run via Deepgram Nova on an already-recorded
    call (uploaded file or a URL to one already hosted, e.g. in a Drive
    dossier, or a SignalWire dialer recording -- see DialerCallAttempt).
    This model only analyzes recordings after the fact; it does not itself
    place, receive, or route calls -- that live telephony path is
    DialerCampaign/DialerCallAttempt via services/signalwire_adapter.py.
    `lead_uid` is nullable because a call may get analyzed before the lead
    is formally intaken off the dialer."""

    __tablename__ = "call_recordings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_uid: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("master_log_entries.lead_uid", ondelete="SET NULL"), index=True
    )
    source_label: Mapped[str] = mapped_column(String(512), nullable=False)
    audio_url: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[CallAnalysisStatus] = mapped_column(
        _pg_enum(CallAnalysisStatus, "call_analysis_status"), nullable=False, default=CallAnalysisStatus.PENDING
    )
    transcript: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[dict | None] = mapped_column(JSONB)
    speakers: Mapped[dict | None] = mapped_column(JSONB)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(10, 2))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WaterwayTelemetrySnapshot(Base):
    """One raw per-poll observation from the river surveillance engine
    (see services/river_surveillance.py) -- a USACE lock-status/queue
    reading or a Datalastic vessel position. Typed columns for the fields
    the friction-index and baseline-learning math actually queries/
    aggregates on; everything else stays in `payload`, same split as
    TelemetryEvent."""

    __tablename__ = "waterway_telemetry_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[TelemetrySource] = mapped_column(_pg_enum(TelemetrySource, "telemetry_source"), nullable=False)
    zone_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    mmsi: Mapped[str | None] = mapped_column(String(32), index=True)
    imo: Mapped[str | None] = mapped_column(String(32), index=True)
    vessel_name: Mapped[str | None] = mapped_column(String(256))
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    speed_knots: Mapped[float | None] = mapped_column(Numeric(6, 2))
    draft_meters: Mapped[float | None] = mapped_column(Numeric(6, 2))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class GeofenceEvent(Base):
    """A classified event derived from WaterwayTelemetrySnapshot rows --
    a vessel entering/exiting a monitored zone, going velocity-anomalous
    inside a restricted channel, queuing at a lock (AIS proxy), or a real
    CWMS gate-change closure. `entity_uid` is set only when cargo
    cross-reference (Import Genius/SeaVantage) resolved a company for the
    vessel -- see river_surveillance.py::_resolve_entity."""

    __tablename__ = "geofence_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    zone_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    event_type: Mapped[GeofenceEventType] = mapped_column(_pg_enum(GeofenceEventType, "geofence_event_type"), nullable=False)
    mmsi: Mapped[str | None] = mapped_column(String(32), index=True)
    imo: Mapped[str | None] = mapped_column(String(32))
    vessel_name: Mapped[str | None] = mapped_column(String(256))
    speed_knots: Mapped[float | None] = mapped_column(Numeric(6, 2))
    stationary_minutes: Mapped[int | None] = mapped_column()
    entity_uid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    detail: Mapped[str | None] = mapped_column(Text)
    source_reference: Mapped[str | None] = mapped_column(String(512))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class WaterwayFrictionMetric(Base):
    """A computed congestion/friction score for one monitored zone at one
    point in time -- composite of USACE lock delay/closure activity and
    Datalastic velocity-anomaly density, see
    river_surveillance.py::compute_friction_index."""

    __tablename__ = "waterway_friction_metrics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    zone_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    friction_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    avg_lock_delay_hours: Mapped[float | None] = mapped_column(Numeric(8, 2))
    active_queue_count: Mapped[int | None] = mapped_column()
    velocity_anomaly_count: Mapped[int | None] = mapped_column()
    baseline_transit_hours: Mapped[float | None] = mapped_column(Numeric(8, 2))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class WaterwayTrigger(Base):
    """One firing of the trigger matrix (distress or expansion). This is
    the actionable output of the whole engine -- when `entity_uid`
    resolves to a real lead, river_surveillance.py appends an audit-trail
    line to that lead's notes via pipeline.update_lead_fields (same path
    every other pipeline mutation uses, so Sheet push + Calendar sync +
    activity logging all fire normally); when it doesn't resolve, the
    trigger still gets recorded here so the signal isn't lost."""

    __tablename__ = "waterway_triggers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trigger_type: Mapped[WaterwayTriggerType] = mapped_column(_pg_enum(WaterwayTriggerType, "waterway_trigger_type"), nullable=False)
    entity_uid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    company_name_guess: Mapped[str | None] = mapped_column(String(256))
    zone_name: Mapped[str | None] = mapped_column(String(256))
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class DialerNumberPool(Base):
    """A named group of SignalWire numbers assigned to campaigns as caller
    ID -- e.g. one pool of local-presence numbers for merchant follow-up,
    a separate pool for pitching a different program, per the request."""

    __tablename__ = "dialer_number_pools"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    numbers: Mapped[list["DialerNumber"]] = relationship(back_populates="pool", cascade="all, delete-orphan")


class DialerNumber(Base):
    """One SignalWire phone number available for outbound caller ID.
    `area_code` drives local-presence selection in services/dialer.py
    (match the lead's own area code where possible); `label` is a free-text
    tag for what the number is used for (e.g. "NY merchant follow-up" vs
    "Program B pitch line") -- numbers themselves are still provisioned in
    the SignalWire dashboard/API, this table just tracks which ones
    Brainboard is allowed to dial out from and why."""

    __tablename__ = "dialer_numbers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pool_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dialer_number_pools.id", ondelete="CASCADE"), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)  # E.164, e.g. +19175551234
    area_code: Mapped[str | None] = mapped_column(String(8), index=True)
    label: Mapped[str | None] = mapped_column(String(256))
    signalwire_number_sid: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    pool: Mapped["DialerNumberPool"] = relationship(back_populates="numbers")


class DialerCampaign(Base):
    """A SignalWire outbound calling campaign. `lead_filter` selects which
    leads populate the dial queue (services/dialer.py::build_dial_queue) --
    e.g. {"calendar": true, "silos": ["CME Macro Funnel"], "master_log_statuses":
    ["New lead"]} -- kept as JSONB rather than a fixed set of columns since
    the source mix (calendar leads / specific Sheet leads / silo-assigned
    candidates) is exactly the kind of ad hoc, evolving filter every other
    JSONB payload/extra_columns field in this schema already exists for.
    `caller_connect_number` is the simple "ring me when the lead answers"
    number -- bridging one outbound call leg to the user's own phone. This
    is NOT the deferred live patch-in/double-dial (bridging a *second*,
    already-in-progress company-dialer call) -- that needs SignalWire
    conferencing and is explicitly a follow-up phase."""

    __tablename__ = "dialer_campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    campaign_type: Mapped[DialerCampaignType] = mapped_column(
        _pg_enum(DialerCampaignType, "dialer_campaign_type"), nullable=False, default=DialerCampaignType.OUTBOUND
    )
    number_pool_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dialer_number_pools.id", ondelete="SET NULL"))
    lead_filter: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    caller_connect_number: Mapped[str | None] = mapped_column(String(32))
    # A publicly reachable URL to a real recording of the broker pitching
    # (not a synthetic/AI voice -- deliberately, see routers/dialer.py's
    # outbound_laml docstring). When set, an answered call plays this and
    # gathers a keypress before bridging; when unset, an answered call
    # bridges straight to caller_connect_number as before (legacy/simple
    # mode). Brainboard doesn't host/upload this file -- paste a link to
    # one already hosted (Drive shareable link, S3, etc.), same "URL, not
    # upload" pattern as CallFromUrlRequest.
    pitch_recording_url: Mapped[str | None] = mapped_column(String(1024))
    max_attempts_per_lead: Mapped[int] = mapped_column(nullable=False, default=3)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    number_pool: Mapped["DialerNumberPool | None"] = relationship()
    attempts: Mapped[list["DialerCallAttempt"]] = relationship(back_populates="campaign", cascade="all, delete-orphan")


class DialerCallAttempt(Base):
    """One placed (or attempted) outbound call. `status` is the raw
    SignalWire call-progress webhook value; `disposition` is the human
    purge/advance decision made from the campaign dashboard afterward --
    see DialerDisposition. Advancing/purging routes through the existing
    pipeline.convert_or_update_silo_candidate / update_lead_status, same
    as the manual silo Convert/Dismiss buttons, so Sheet push + Calendar
    sync + activity logging all fire normally regardless of whether the
    mutation originated from a click or a call outcome."""

    __tablename__ = "dialer_call_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable: an inbound call isn't placed by any outbound campaign --
    # see direction below. Every outbound attempt still always has one.
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dialer_campaigns.id", ondelete="CASCADE"), index=True)
    direction: Mapped[DialerCallDirection] = mapped_column(
        _pg_enum(DialerCallDirection, "dialer_call_direction"), nullable=False, default=DialerCallDirection.OUTBOUND
    )
    lead_uid: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("master_log_entries.lead_uid", ondelete="SET NULL"), index=True
    )
    silo_candidate_uid: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("silo_candidates.candidate_uid", ondelete="SET NULL"), index=True
    )
    from_number_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dialer_numbers.id", ondelete="SET NULL"))
    to_number: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_number: Mapped[int] = mapped_column(nullable=False, default=1)
    signalwire_call_sid: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[DialerCallStatus] = mapped_column(
        _pg_enum(DialerCallStatus, "dialer_call_status"), nullable=False, default=DialerCallStatus.QUEUED
    )
    recording_url: Mapped[str | None] = mapped_column(String(1024))
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(10, 2))
    disposition: Mapped[DialerDisposition] = mapped_column(
        _pg_enum(DialerDisposition, "dialer_disposition"), nullable=False, default=DialerDisposition.UNSET
    )
    # Inbound-only: which ring-group target actually answered, set by the
    # per-leg statusCallback in the inbound webhook flow (see
    # services/dialer.py's ring-group docstring for what's confirmed vs
    # assumed about that mechanism). NULL for outbound, and NULL for an
    # inbound call nobody picked up (falls to voicemail instead).
    answered_by_co_broker: Mapped[CoBroker | None] = mapped_column(_pg_enum(CoBroker, "co_broker"))
    error: Mapped[str | None] = mapped_column(Text)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    campaign: Mapped["DialerCampaign | None"] = relationship(back_populates="attempts")


class InboundRingTarget(Base):
    """One phone that rings when a merchant calls a SignalWire number back
    -- the "5 cell phones" (you + 4 brokers) ring simultaneously; whoever
    answers first gets connected, the rest stop ringing automatically
    (see services/dialer.py's ring-group builder). co_broker links the
    ringing phone to a name for attribution -- see LeadContributor."""

    __tablename__ = "inbound_ring_targets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    co_broker: Mapped[CoBroker] = mapped_column(_pg_enum(CoBroker, "co_broker"), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)  # E.164, e.g. +19175551234
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeadContributor(Base):
    """Additive credit ledger for a lead beyond its single primary
    MasterLogEntry.co_broker -- e.g. "whoever answers an inbound call
    gets added to the deal, there's enough $ to go around" per the
    explicit product decision this was built from. Never replaces or
    mutates the primary co_broker column; this is purely additive so
    commission-split reporting has a real, queryable trail of who
    touched a deal and why, instead of a single mutable owner field that
    can only ever tell one story."""

    __tablename__ = "lead_contributors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_uid: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("master_log_entries.lead_uid", ondelete="CASCADE"), nullable=False, index=True
    )
    co_broker: Mapped[CoBroker] = mapped_column(_pg_enum(CoBroker, "co_broker"), nullable=False)
    reason: Mapped[LeadContributionReason] = mapped_column(
        _pg_enum(LeadContributionReason, "lead_contribution_reason"), nullable=False
    )
    credited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
