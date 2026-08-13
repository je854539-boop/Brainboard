from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.orm import GlobeSignal
from app.schemas import GlobeSignalOut
from app.services.globe import all_globe_adapters

router = APIRouter(prefix="/api/globe", tags=["globe"])


@router.get("/signals", response_model=list[GlobeSignalOut])
def list_signals(limit: int = 2000, db: Session = Depends(get_db)):
    query = select(GlobeSignal).order_by(GlobeSignal.observed_at.desc()).limit(min(limit, 5000))
    return db.execute(query).scalars().all()


@router.get("/sources")
def globe_sources():
    return [{"source": a.source.value, "enabled": a.enabled} for a in all_globe_adapters()]


@router.post("/refresh")
def refresh_signals(db: Session = Depends(get_db)):
    ingested = {adapter.source.value: adapter.ingest(db) for adapter in all_globe_adapters()}
    return {"ingested": ingested}
