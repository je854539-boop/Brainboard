import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import parse_optional_enum
from app.models.enums import ActivitySource, SiloCandidateStatus, SiloName
from app.models.orm import SiloCandidate
from app.schemas import SiloCandidateOut, SiloCandidateUpdate
from app.services import pipeline, silo_leadgen

router = APIRouter(prefix="/api/silo", tags=["silo"])


@router.post("/leadgen/run")
def run_leadgen_sweep(db: Session = Depends(get_db)):
    """'The brain does lead gen through the silos': runs every configured
    macro telemetry adapter and derives SiloCandidate rows from what it
    ingests. Safe no-op for any source without an API key configured."""
    return silo_leadgen.run_silo_leadgen(db)


@router.get("", response_model=list[SiloCandidateOut])
def list_silo_candidates(
    silo: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    silo = parse_optional_enum(silo, SiloName)
    status = parse_optional_enum(status, SiloCandidateStatus)

    query = select(SiloCandidate)
    if silo:
        query = query.where(SiloCandidate.silo == silo)
    if status:
        query = query.where(SiloCandidate.status == status)
    query = query.order_by(SiloCandidate.score.desc().nullslast(), SiloCandidate.updated_at.desc())
    return db.execute(query).scalars().all()


@router.get("/geo")
def silo_candidates_geo(db: Session = Depends(get_db)):
    """Geo-anchored silo candidates for the globe's silo-funnel-lifecycle
    layer (creation -> conversion into Master Log V2 -> funded). Only
    returns candidates with real coordinates on file (see
    silo_leadgen.py::_regrid_geometry_centroid) -- most silos have no
    inherent geo signal in their source data and simply don't appear here
    rather than being placed at a guessed location."""
    candidates = (
        db.query(SiloCandidate)
        .filter(SiloCandidate.latitude.isnot(None), SiloCandidate.longitude.isnot(None))
        .all()
    )
    return [
        {
            "candidate_uid": str(c.candidate_uid),
            "silo": c.silo.value,
            "company_name": c.company_name,
            "status": c.status.value,
            "score": float(c.score) if c.score is not None else None,
            "latitude": float(c.latitude),
            "longitude": float(c.longitude),
            "converted_lead_uid": str(c.converted_lead_uid) if c.converted_lead_uid else None,
            "source_reference": c.source_reference,
        }
        for c in candidates
    ]


@router.get("/{candidate_uid}", response_model=SiloCandidateOut)
def get_silo_candidate(candidate_uid: uuid.UUID, db: Session = Depends(get_db)):
    candidate = db.get(SiloCandidate, candidate_uid)
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    return candidate


@router.patch("/{candidate_uid}", response_model=SiloCandidateOut)
def update_silo_candidate(candidate_uid: uuid.UUID, payload: SiloCandidateUpdate, db: Session = Depends(get_db)):
    candidate = db.get(SiloCandidate, candidate_uid)
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate not found")

    try:
        return pipeline.convert_or_update_silo_candidate(
            db, candidate, payload.status, payload.co_broker, source=ActivitySource.UI
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
