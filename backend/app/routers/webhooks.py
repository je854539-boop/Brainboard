"""Endpoints called by the Google Apps Script bindings (see
apps_script/Code.gs): doPost-style lead intake, onMasterLogEdit (Column
K/L -> Calendar sync), and onSiloStatusEdit (convert/dismiss candidates).
All routes require the `X-Webhook-Secret` header to match
WEBHOOK_SHARED_SECRET.

Every mutation here is tagged ActivitySource.WEBHOOK_SHEET so
pipeline.push_to_sheet skips writing back to the Sheet for changes that
originated FROM the Sheet -- otherwise a Sheet edit would round-trip back
into an infinite Sheet<->Postgres sync loop.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import verify_webhook_secret
from app.models.enums import ActivitySource
from app.models.orm import MasterLogEntry, SiloCandidate
from app.schemas import MasterLogEditWebhook, MasterLogEntryOut, MasterLogEntryUpdate, SiloCandidateOut, SiloStatusEditWebhook
from app.services import pipeline

router = APIRouter(prefix="/webhooks", tags=["webhooks"], dependencies=[Depends(verify_webhook_secret)])


@router.post("/lead-intake", response_model=MasterLogEntryOut, status_code=201)
def lead_intake(payload: MasterLogEntryUpdate, db: Session = Depends(get_db)):
    """doPost equivalent: create a new Master Log lead (e.g. from an
    inbound web form)."""
    if not payload.business_name or not payload.co_broker:
        raise HTTPException(status_code=422, detail="business_name and co_broker are required")

    return pipeline.create_lead(
        db,
        business_name=payload.business_name,
        co_broker=payload.co_broker,
        source=ActivitySource.WEBHOOK_SHEET,
        **payload.model_dump(exclude={"business_name", "co_broker"}, exclude_none=True),
    )


@router.post("/master-log-edit", response_model=MasterLogEntryOut)
def master_log_edit(payload: MasterLogEditWebhook, db: Session = Depends(get_db)):
    """onMasterLogEdit: Column K (follow-up date) or Column L (notes)
    changed on the sheet -- push the update into Postgres and reflect it
    onto the linked Calendar event via lead_uid."""
    entry = db.get(MasterLogEntry, payload.lead_uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="lead not found")

    updates = {}
    if payload.follow_up_date is not None:
        updates["follow_up_date"] = payload.follow_up_date
    if payload.notes is not None:
        updates["notes"] = payload.notes

    return pipeline.update_lead_fields(db, entry, updates, source=ActivitySource.WEBHOOK_SHEET)


@router.post("/silo-status-edit", response_model=SiloCandidateOut)
def silo_status_edit(payload: SiloStatusEditWebhook, db: Session = Depends(get_db)):
    """onSiloStatusEdit: a macro silo candidate was marked
    converted/dismissed/pending on the sheet -- mirror it in Postgres,
    materializing a Master Log lead on conversion."""
    candidate = db.get(SiloCandidate, payload.candidate_uid)
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate not found")

    try:
        return pipeline.convert_or_update_silo_candidate(
            db, candidate, payload.status, payload.co_broker, source=ActivitySource.WEBHOOK_SHEET
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
