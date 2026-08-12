import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import parse_optional_enum
from app.models.enums import CoBroker, MasterLogStatus
from app.models.orm import MasterLogEntry
from app.schemas import MasterLogEntryOut, MasterLogEntryUpdate
from app.services import pipeline

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


@router.get("/{lead_uid}", response_model=MasterLogEntryOut)
def get_master_log_entry(lead_uid: uuid.UUID, db: Session = Depends(get_db)):
    entry = db.get(MasterLogEntry, lead_uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return entry


@router.post("", response_model=MasterLogEntryOut, status_code=201)
def create_master_log_entry(payload: MasterLogEntryUpdate, db: Session = Depends(get_db)):
    if not payload.business_name or not payload.co_broker:
        raise HTTPException(status_code=422, detail="business_name and co_broker are required")

    return pipeline.create_lead(
        db,
        business_name=payload.business_name,
        co_broker=payload.co_broker,
        **payload.model_dump(exclude={"business_name", "co_broker"}, exclude_none=True),
    )


@router.patch("/{lead_uid}", response_model=MasterLogEntryOut)
def update_master_log_entry(lead_uid: uuid.UUID, payload: MasterLogEntryUpdate, db: Session = Depends(get_db)):
    entry = db.get(MasterLogEntry, lead_uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="lead not found")

    updates = payload.model_dump(exclude_unset=True)
    new_status = updates.pop("status", None)

    for field, value in updates.items():
        setattr(entry, field, value)
    db.commit()

    if new_status is not None:
        pipeline.update_lead_status(db, entry, new_status)

    db.refresh(entry)
    return entry
