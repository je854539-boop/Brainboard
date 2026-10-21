import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import parse_optional_enum
from app.models.enums import ActivityEventType, ActivitySource, CoBroker, MasterLogStatus
from app.models.orm import FundingEvent, MasterLogEntry
from app.schemas import ClickEvent, FundingEventCreate, FundingEventOut, MasterLogEntryOut, MasterLogEntryUpdate
from app.services import floor_language, pipeline
from app.services.google.sheets_sync import _parse_funded_date

router = APIRouter(prefix="/api/master-log", tags=["master-log"])


@router.get("", response_model=list[MasterLogEntryOut])
def list_master_log(
    co_broker: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    co_broker = parse_optional_enum(co_broker, CoBroker)
    status = parse_optional_enum(status, MasterLogStatus)

    query = select(MasterLogEntry)
    if co_broker:
        query = query.where(MasterLogEntry.co_broker == co_broker)
    if status:
        query = query.where(MasterLogEntry.status == status)
    query = query.order_by(MasterLogEntry.updated_at.desc())
    return db.execute(query).scalars().all()


@router.get("/duplicates")
def get_duplicate_leads(db: Session = Depends(get_db)):
    """Report-only (see pipeline.find_duplicate_leads's docstring for why
    this never auto-merges): groups of leads that look like the same
    merchant entered more than once, by phone (high confidence) and by
    normalized business name (lower confidence, name-only)."""
    return pipeline.find_duplicate_leads(db)


@router.get("/offers-pending-funding")
def get_offers_pending_funding(db: Session = Depends(get_db)):
    """Leads sitting in Offer Made Not Sold -- an offer is out, the deal
    hasn't closed, oldest-standing offer first. See
    pipeline.offers_pending_funding."""
    return pipeline.offers_pending_funding(db)


@router.get("/{lead_uid}", response_model=MasterLogEntryOut)
def get_master_log_entry(lead_uid: uuid.UUID, db: Session = Depends(get_db)):
    entry = db.get(MasterLogEntry, lead_uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return entry


@router.get("/{lead_uid}/funding-events", response_model=list[FundingEventOut])
def list_funding_events(lead_uid: uuid.UUID, db: Session = Depends(get_db)):
    """Full funding history for this lead/merchant, oldest first -- a
    repeat merchant funded 4 times has 4 rows here, nothing overwritten.
    See pipeline.record_funding_event for how a new one gets added."""
    entry = db.get(MasterLogEntry, lead_uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return db.execute(
        select(FundingEvent).where(FundingEvent.lead_uid == lead_uid).order_by(FundingEvent.funded_date, FundingEvent.created_at)
    ).scalars().all()


@router.post("/{lead_uid}/funding-events", response_model=FundingEventOut, status_code=201)
def create_funding_event(lead_uid: uuid.UUID, payload: FundingEventCreate, db: Session = Depends(get_db)):
    """Records a new funding for this merchant -- appends to the history,
    never overwrites a prior funding. Also updates MasterLogEntry's own
    original_funded_amount/funded_date/payment fields to this (the
    newest) event -- see pipeline.record_funding_event's docstring for
    why that denormalization exists."""
    entry = db.get(MasterLogEntry, lead_uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="lead not found")

    funded_date, funded_date_precision = (None, None)
    if payload.funded_date:
        funded_date, funded_date_precision = _parse_funded_date(payload.funded_date)
        if funded_date is None:
            raise HTTPException(status_code=422, detail=f"unparseable funded_date: {payload.funded_date!r} (use M/D/YYYY or M/YYYY)")

    return pipeline.record_funding_event(
        db, entry, payload.funded_amount, funded_date=funded_date, funded_date_precision=funded_date_precision,
        term_weeks=payload.term_weeks, factor_rate=payload.factor_rate, payment_amt=payload.payment_amt,
        payment_freq=payload.payment_freq, lender=payload.lender, notes=payload.notes,
    )


@router.get("/{lead_uid}/floor-signals")
def get_floor_signals(lead_uid: uuid.UUID, db: Session = Depends(get_db)):
    """Lexicon-extracted signal from this lead's own notes (see
    services/floor_language.py) -- computed fresh on every call, not
    persisted, since it's cheap local string matching with no external
    dependency to decouple from."""
    entry = db.get(MasterLogEntry, lead_uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return {
        **floor_language.extract_signals(entry.business_name, entry.notes),
        "timeline": floor_language.extract_timeline(entry.notes),
        "calculated_payoff": floor_language.project_payoff(
            float(entry.current_balance) if entry.current_balance is not None else None,
            float(entry.payment_amt) if entry.payment_amt is not None else None,
            entry.payment_freq,
        ),
        "merchant_stated_readiness": floor_language.extract_merchant_stated_readiness(entry.notes),
        "additional_position_eligibility": floor_language.additional_position_eligibility(
            float(entry.current_balance) if entry.current_balance is not None else None,
            float(entry.payment_amt) if entry.payment_amt is not None else None,
            entry.payment_freq,
            entry.open_positions,
            entry.notes,
            float(entry.original_funded_amount) if entry.original_funded_amount is not None else None,
        ),
    }


@router.post("", response_model=MasterLogEntryOut, status_code=201)
def create_master_log_entry(payload: MasterLogEntryUpdate, db: Session = Depends(get_db)):
    if not payload.business_name or not payload.co_broker:
        raise HTTPException(status_code=422, detail="business_name and co_broker are required")

    return pipeline.create_lead(
        db,
        business_name=payload.business_name,
        co_broker=payload.co_broker,
        source=ActivitySource.UI,
        **payload.model_dump(exclude={"business_name", "co_broker"}, exclude_none=True),
    )


@router.patch("/{lead_uid}", response_model=MasterLogEntryOut)
def update_master_log_entry(lead_uid: uuid.UUID, payload: MasterLogEntryUpdate, db: Session = Depends(get_db)):
    entry = db.get(MasterLogEntry, lead_uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="lead not found")

    updates = payload.model_dump(exclude_unset=True, exclude={"lead_uid"})
    return pipeline.update_lead_fields(db, entry, updates, source=ActivitySource.UI)


@router.post("/{lead_uid}/activity", status_code=204)
def log_click(lead_uid: uuid.UUID, payload: ClickEvent, db: Session = Depends(get_db)):
    """Fire-and-forget UI instrumentation ('every click') -- the dashboard
    beacons here on dossier-link opens, row expansions, etc."""
    entry = db.get(MasterLogEntry, lead_uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="lead not found")
    pipeline.log_activity(db, lead_uid, ActivityEventType.CLICK, ActivitySource.UI, field_name=payload.label)
    db.commit()
