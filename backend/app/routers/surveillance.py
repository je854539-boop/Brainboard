from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import parse_optional_enum
from app.models.enums import TelemetrySource
from app.models.orm import TelemetryEvent
from app.schemas import TelemetryEventOut

router = APIRouter(prefix="/api/surveillance", tags=["surveillance"])


@router.get("/feed", response_model=list[TelemetryEventOut])
def surveillance_feed(source: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
    source = parse_optional_enum(source, TelemetrySource)
    query = select(TelemetryEvent)
    if source:
        query = query.where(TelemetryEvent.source == source)
    query = query.order_by(TelemetryEvent.ingested_at.desc()).limit(min(limit, 200))
    return db.execute(query).scalars().all()
