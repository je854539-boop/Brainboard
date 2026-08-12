import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import CoBroker, MasterLogStatus, SiloCandidateStatus, SiloName, TelemetrySource


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
    loan_amount_requested: float | None
    follow_up_date: datetime | None
    notes: str | None
    dossier_drive_link: str | None
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
    loan_amount_requested: float | None = None
    follow_up_date: datetime | None = None
    notes: str | None = None
    dossier_drive_link: str | None = None


class SiloCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    candidate_uid: uuid.UUID
    silo: SiloName
    company_name: str
    contact_name: str | None
    phone: str | None
    source_reference: str | None
    notes: str | None
    dossier_drive_link: str | None
    score: float | None
    status: SiloCandidateStatus
    converted_lead_uid: uuid.UUID | None
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
