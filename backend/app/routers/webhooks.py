"""Endpoints called by the Google Apps Script bindings (see
apps_script/Code.gs): doPost-style lead intake, onMasterLogEdit (Column
K/L -> Calendar sync), and onSiloStatusEdit (convert/dismiss candidates).
All routes require the `X-Webhook-Secret` header to match
WEBHOOK_SHARED_SECRET."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import verify_webhook_secret
from app.models.orm import MasterLogEntry, SiloCandidate
from app.schemas import MasterLogEditWebhook, MasterLogEntryOut, MasterLogEntryUpdate, SiloCandidateOut, SiloStatusEditWebhook
from app.services import pipeline
from app.services.google import auth as google_auth
from app.services.google import calendar_sync

logger = logging.getLogger("brainboard.webhooks")

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

    if payload.follow_up_date is not None:
        entry.follow_up_date = payload.follow_up_date
    if payload.notes is not None:
        entry.notes = payload.notes
    db.commit()
    db.refresh(entry)

    if google_auth.is_configured():
        try:
            event_id = calendar_sync.upsert_follow_up_event(google_auth.calendar_service(), entry)
            entry.calendar_event_id = event_id
            db.commit()
            db.refresh(entry)
        except Exception:
            logger.exception("Calendar sync failed for lead %s", entry.lead_uid)
    else:
        logger.info("Google sync not configured -- skipping Calendar push for lead %s", entry.lead_uid)

    return entry


@router.post("/silo-status-edit", response_model=SiloCandidateOut)
def silo_status_edit(payload: SiloStatusEditWebhook, db: Session = Depends(get_db)):
    """onSiloStatusEdit: a macro silo candidate was marked
    converted/dismissed/pending on the sheet -- mirror it in Postgres,
    materializing a Master Log lead on conversion."""
    candidate = db.get(SiloCandidate, payload.candidate_uid)
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate not found")

    try:
        return pipeline.convert_or_update_silo_candidate(db, candidate, payload.status, payload.co_broker)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
