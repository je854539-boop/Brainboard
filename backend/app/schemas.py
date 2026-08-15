import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import (
    CallAnalysisStatus,
    CoBroker,
    DialerCallStatus,
    DialerCampaignType,
    DialerDisposition,
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
    follow_up_date: datetime | None
    notes: str | None
    dossier_drive_link: str | None
    financials_link: str | None
    transcripts_link: str | None
    calendar_event_id: str | None
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


class MasterLogEditWebhook(BaseModel):
    lead_uid: uuid.UUID
    follow_up_date: datetime | None = None
    notes: str | None = None


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
    created_at: datetime
    numbers: list[DialerNumberOut] = []


class DialerNumberPoolCreate(BaseModel):
    name: str
    description: str | None = None


class DialerCampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    campaign_type: DialerCampaignType
    number_pool_id: uuid.UUID | None
    lead_filter: dict
    caller_connect_number: str | None
    pitch_recording_url: str | None
    max_attempts_per_lead: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class DialerCampaignCreate(BaseModel):
    name: str
    campaign_type: DialerCampaignType = DialerCampaignType.OUTBOUND
    number_pool_id: uuid.UUID | None = None
    lead_filter: dict = {}
    caller_connect_number: str | None = None
    pitch_recording_url: str | None = None
    max_attempts_per_lead: int = 3


class DialerCampaignUpdate(BaseModel):
    name: str | None = None
    number_pool_id: uuid.UUID | None = None
    lead_filter: dict | None = None
    caller_connect_number: str | None = None
    pitch_recording_url: str | None = None
    max_attempts_per_lead: int | None = None
    is_active: bool | None = None


class DialerCallAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    campaign_id: uuid.UUID
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
    error: str | None
    placed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DialerDispositionUpdate(BaseModel):
    disposition: DialerDisposition
    co_broker: CoBroker | None = None  # required only when disposition=advance on a silo candidate


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
