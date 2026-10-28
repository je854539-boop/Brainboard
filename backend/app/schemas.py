import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import (
    CallAnalysisStatus,
    CoBroker,
    DatePrecision,
    DialerCallDirection,
    DialerCallStatus,
    DialerCampaignType,
    DialerDisposition,
    LeadContributionReason,
    MasterLogStatus,
    SiloCandidateStatus,
    SiloName,
    TelemetrySource,
)


class HazardSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    survival_probability: float
    days_in_stage: int
    next_stage_transition_probability: float | None
    computed_at: datetime


class MasterLogEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    lead_uid: uuid.UUID
    business_name: str
    contact_name: str | None
    phone: str | None
    email: str | None
    co_broker: CoBroker
    status: MasterLogStatus
    state: str | None
    annual_revenue: float | None
    lender: str | None
    payment_amt: float | None
    payment_freq: str | None
    current_balance: float | None
    open_positions: int | None
    credit_score: int | None
    original_funded_amount: float | None
    funded_date: date | None
    funded_date_precision: DatePrecision | None
    follow_up_date: datetime | None
    notes: str | None
    dossier_drive_link: str | None
    financials_link: str | None
    transcripts_link: str | None
    calendar_event_id: str | None
    source_silo: SiloName | None
    source_channel: TelemetrySource | None
    created_at: datetime
    updated_at: datetime
    hazard_snapshot: HazardSnapshotOut | None = None


class MasterLogEntryUpdate(BaseModel):
    lead_uid: uuid.UUID | None = None  # set only when a caller (e.g. Apps Script doPost) pre-assigned the UUID
    business_name: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    co_broker: CoBroker | None = None
    status: MasterLogStatus | None = None
    state: str | None = None
    annual_revenue: float | None = None
    lender: str | None = None
    payment_amt: float | None = None
    payment_freq: str | None = None
    current_balance: float | None = None
    open_positions: int | None = None
    credit_score: int | None = None
    original_funded_amount: float | None = None
    funded_date: date | None = None
    funded_date_precision: DatePrecision | None = None
    follow_up_date: datetime | None = None
    notes: str | None = None
    dossier_drive_link: str | None = None
    financials_link: str | None = None
    transcripts_link: str | None = None


class SiloCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    candidate_uid: uuid.UUID
    silo: SiloName
    company_name: str
    contact_name: str | None
    phone: str | None
    email: str | None
    source_reference: str | None
    notes: str | None
    dossier_drive_link: str | None
    score: float | None
    status: SiloCandidateStatus
    origin_source: TelemetrySource | None
    converted_lead_uid: uuid.UUID | None
    latitude: float | None
    longitude: float | None
    created_at: datetime
    updated_at: datetime


class SiloCandidateUpdate(BaseModel):
    status: SiloCandidateStatus
    co_broker: CoBroker | None = None  # required to materialize a MasterLogEntry when converting


class TelemetryEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: TelemetrySource
    entity_uid: uuid.UUID | None
    title: str
    payload: dict
    ingested_at: datetime


class SurvivalPointOut(BaseModel):
    t_days: float
    survival_probability: float


class FundingEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_uid: uuid.UUID
    funded_amount: float
    funded_date: date | None
    funded_date_precision: DatePrecision | None
    term_weeks: float | None
    factor_rate: float | None
    payment_amt: float | None
    payment_freq: str | None
    lender: str | None
    position_number: int | None
    notes: str | None
    created_at: datetime


class FundingEventCreate(BaseModel):
    funded_amount: float
    # "M/D/YYYY" or "M/YYYY" -- parsed the same way as the sheet's Funded
    # Date column (see sheets_sync._parse_funded_date), so a month-only
    # entry is never silently promoted to a fake exact date.
    funded_date: str | None = None
    term_weeks: float | None = None
    factor_rate: float | None = None
    payment_amt: float | None = None
    payment_freq: str | None = None
    lender: str | None = None
    notes: str | None = None


class MasterLogEditWebhook(BaseModel):
    lead_uid: uuid.UUID
    follow_up_date: datetime | None = None
    notes: str | None = None
    original_funded_amount: float | None = None
    # Raw sheet text ("M/D/YYYY" or "M/YYYY") -- parsed server-side via
    # sheets_sync._parse_funded_date, same as a full-sheet pull, so a
    # month-only entry is never silently promoted to a fake exact date.
    funded_date: str | None = None
    # Raw sheet text, validated against MasterLogStatus server-side (see
    # dependencies.parse_optional_enum) rather than coerced here, so an
    # invalid value 422s with a clear message instead of a generic
    # Pydantic validation error.
    status: str | None = None


class SiloStatusEditWebhook(BaseModel):
    candidate_uid: uuid.UUID
    status: SiloCandidateStatus
    co_broker: CoBroker | None = None


class ClickEvent(BaseModel):
    label: str  # e.g. "dossier_link_opened", "row_expanded", "hazard_tooltip_viewed"


class EnrichmentResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_uid: uuid.UUID
    source: TelemetrySource
    payload: dict
    confidence: float | None
    error: str | None
    created_at: datetime


class CSVImportSummary(BaseModel):
    created: int
    skipped: int
    errors: list[str]


class GlobeSignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: TelemetrySource
    latitude: float
    longitude: float
    intensity: float | None
    title: str
    payload: dict
    entity_uid: uuid.UUID | None
    observed_at: datetime


class CallRecordingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_uid: uuid.UUID | None
    source_label: str
    audio_url: str | None
    status: CallAnalysisStatus
    transcript: str | None
    summary: str | None
    sentiment: dict | None
    speakers: dict | list | None
    duration_seconds: float | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class CallFromUrlRequest(BaseModel):
    audio_url: str
    lead_uid: uuid.UUID | None = None
    source_label: str | None = None


class DialerNumberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    pool_id: uuid.UUID
    phone_number: str
    area_code: str | None
    label: str | None
    signalwire_number_sid: str | None
    is_active: bool
    created_at: datetime


class DialerNumberCreate(BaseModel):
    pool_id: uuid.UUID
    phone_number: str
    area_code: str | None = None
    label: str | None = None
    signalwire_number_sid: str | None = None


class DialerNumberPoolOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    industry_tag: str | None
    created_at: datetime
    numbers: list[DialerNumberOut] = []


class DialerNumberPoolCreate(BaseModel):
    name: str
    description: str | None = None
    industry_tag: str | None = None


class DialerCampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    campaign_type: DialerCampaignType
    number_pool_id: uuid.UUID | None
    lead_filter: dict
    caller_connect_number: str | None
    bridge_to_ring_roster: bool
    pitch_recording_url: str | None
    max_attempts_per_lead: int
    max_list_size: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class DialerCampaignCreate(BaseModel):
    name: str
    campaign_type: DialerCampaignType = DialerCampaignType.OUTBOUND
    number_pool_id: uuid.UUID | None = None
    lead_filter: dict = {}
    caller_connect_number: str | None = None
    # When true, an answered outbound call rings every active
    # InboundRingTarget simultaneously (same ring-group pattern as
    # inbound) instead of the single caller_connect_number -- "first
    # available desk takes the live lead." False keeps the existing
    # single-number bridge behavior exactly as before.
    bridge_to_ring_roster: bool = False
    pitch_recording_url: str | None = None
    max_attempts_per_lead: int = 3
    max_list_size: int = 1000

    @field_validator("max_list_size")
    @classmethod
    def _clamp_max_list_size(cls, v: int) -> int:
        # Hard compliance ceiling -- see services/dialer.py::HARD_MAX_LIST_SIZE.
        # A request can only ever tighten this, never raise it past 1000.
        return max(1, min(v, 1000))


class DialerCampaignUpdate(BaseModel):
    name: str | None = None
    number_pool_id: uuid.UUID | None = None
    lead_filter: dict | None = None
    caller_connect_number: str | None = None
    bridge_to_ring_roster: bool | None = None
    pitch_recording_url: str | None = None
    max_attempts_per_lead: int | None = None
    max_list_size: int | None = None
    is_active: bool | None = None

    @field_validator("max_list_size")
    @classmethod
    def _clamp_max_list_size(cls, v: int | None) -> int | None:
        return v if v is None else max(1, min(v, 1000))


class DialerCallAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    campaign_id: uuid.UUID | None
    direction: DialerCallDirection
    lead_uid: uuid.UUID | None
    silo_candidate_uid: uuid.UUID | None
    from_number_id: uuid.UUID | None
    to_number: str
    attempt_number: int
    signalwire_call_sid: str | None
    status: DialerCallStatus
    recording_url: str | None
    duration_seconds: float | None
    disposition: DialerDisposition
    answered_by_co_broker: CoBroker | None
    error: str | None
    placed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DialerDispositionUpdate(BaseModel):
    disposition: DialerDisposition
    co_broker: CoBroker | None = None  # required only when disposition=advance on a silo candidate


class InboundRingTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    co_broker: CoBroker
    phone_number: str
    is_active: bool
    created_at: datetime


class InboundRingTargetCreate(BaseModel):
    co_broker: CoBroker
    phone_number: str


class LeadContributorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_uid: uuid.UUID
    co_broker: CoBroker
    reason: LeadContributionReason
    credited_at: datetime


class DialerQueuePreviewEntry(BaseModel):
    lead_source: str  # "calendar" | "sheet_lead" | "silo_candidate"
    entity_uid: uuid.UUID
    company_name: str
    to_number: str | None
    attempts_so_far: int


class DialerCallNowRequest(BaseModel):
    """Ad hoc, single-target call triggered by a click-to-call button
    (Master Log row / globe entity panel) rather than a campaign sweep --
    exactly one of these two must be set. Reuses the target campaign's
    number pool / caller_connect_number / pitch_recording_url as-is."""

    lead_uid: uuid.UUID | None = None
    silo_candidate_uid: uuid.UUID | None = None


class DialerCallStatusWebhook(BaseModel):
    """Shape of SignalWire's Twilio-compatible call-status callback --
    only the fields the dialer actually consumes; SignalWire posts more
    that we don't need. See routers/dialer.py."""

    CallSid: str
    CallStatus: str
    CallDuration: str | None = None
    RecordingUrl: str | None = None
