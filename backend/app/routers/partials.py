"""HTMX partial-fragment endpoints -- return table-row HTML instead of
JSON, swapped into the dashboard pages on filter change / polling."""

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import parse_optional_enum
from app.models.enums import CoBroker, MasterLogStatus, SiloCandidateStatus, SiloName, TelemetrySource
from app.models.orm import MasterLogEntry, SiloCandidate, TelemetryEvent

ALL_CO_BROKERS = list(CoBroker)

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(prefix="/partials", tags=["partials"])


@router.get("/master-log-rows")
def master_log_rows(
    request: Request,
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
    entries = db.execute(query).scalars().all()
    return templates.TemplateResponse(request, "partials/master_log_rows.html", {"entries": entries})


@router.get("/silo-rows")
def silo_rows(
    request: Request,
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
    candidates = db.execute(query).scalars().all()
    return templates.TemplateResponse(
        request, "partials/silo_rows.html", {"candidates": candidates, "co_brokers": ALL_CO_BROKERS}
    )


@router.get("/surveillance-feed")
def surveillance_feed(request: Request, source: str | None = None, db: Session = Depends(get_db)):
    source = parse_optional_enum(source, TelemetrySource)
    query = select(TelemetryEvent)
    if source:
        query = query.where(TelemetryEvent.source == source)
    query = query.order_by(TelemetryEvent.ingested_at.desc()).limit(100)
    events = db.execute(query).scalars().all()
    return templates.TemplateResponse(request, "partials/surveillance_feed.html", {"events": events})
