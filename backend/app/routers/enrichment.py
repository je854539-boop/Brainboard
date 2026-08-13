import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.orm import EnrichmentResult, MasterLogEntry
from app.schemas import CSVImportSummary, EnrichmentResultOut
from app.services import enrichment_orchestrator
from app.services.enrichment import all_enrichment_adapters

router = APIRouter(prefix="/api/enrichment", tags=["enrichment"])


@router.get("/sources")
def enrichment_sources():
    return [{"source": a.source.value, "enabled": a.enabled} for a in all_enrichment_adapters()]


@router.post("/upload", response_model=CSVImportSummary)
async def upload_leads_csv(file: UploadFile, db: Session = Depends(get_db)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=422, detail="expected a .csv file")
    content = await file.read()
    summary = enrichment_orchestrator.import_leads_csv(db, content)
    return summary


@router.post("/run/{lead_uid}", response_model=list[EnrichmentResultOut])
def run_enrichment(lead_uid: uuid.UUID, db: Session = Depends(get_db)):
    lead = db.get(MasterLogEntry, lead_uid)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return enrichment_orchestrator.enrich_lead(db, lead)


@router.get("/results", response_model=list[EnrichmentResultOut])
def list_enrichment_results(entity_uid: uuid.UUID | None = None, limit: int = 50, db: Session = Depends(get_db)):
    query = select(EnrichmentResult)
    if entity_uid:
        query = query.where(EnrichmentResult.entity_uid == entity_uid)
    query = query.order_by(EnrichmentResult.created_at.desc()).limit(min(limit, 200))
    return db.execute(query).scalars().all()
