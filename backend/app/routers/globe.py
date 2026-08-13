from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import CallAnalysisStatus
from app.models.orm import CallRecording, GlobeSignal, LeadActivityEvent, MasterLogEntry, ShadowScore
from app.schemas import GlobeSignalOut
from app.services import globe_geo
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


@router.get("/leads")
def globe_leads(db: Session = Depends(get_db)):
    """Every lead plotted on the globe by its Master Log V2 status,
    positioned at its state's approximate centroid (see
    services/globe_geo.py -- state-level placement, not an exact
    geocoded address, since MasterLogEntry doesn't store one). Skips
    leads with no state on file or a state code not in the reference
    table, rather than guessing a position.

    `behavioral_signals` surfaces real engagement/audit data already
    tracked on the lead -- activity-ledger event counts, the Brain's
    latest funded-probability score, and Deepgram call-sentiment when a
    call's been analyzed. This is NOT biometric data -- Brainboard has no
    biometric data source anywhere in the system, and none of the above
    should be represented as one."""
    leads = db.query(MasterLogEntry).all()
    results = []
    for lead in leads:
        coords = globe_geo.state_centroid(lead.state)
        if coords is None:
            continue

        activity_counts = dict(
            db.query(LeadActivityEvent.event_type, func.count())
            .filter(LeadActivityEvent.lead_uid == lead.lead_uid)
            .group_by(LeadActivityEvent.event_type)
            .all()
        )
        latest_score = (
            db.query(ShadowScore)
            .filter(ShadowScore.lead_uid == lead.lead_uid)
            .order_by(ShadowScore.computed_at.desc())
            .first()
        )
        latest_call = (
            db.query(CallRecording)
            .filter(CallRecording.lead_uid == lead.lead_uid, CallRecording.status == CallAnalysisStatus.COMPLETED)
            .order_by(CallRecording.created_at.desc())
            .first()
        )

        results.append(
            {
                "lead_uid": str(lead.lead_uid),
                "business_name": lead.business_name,
                "status": lead.status.value,
                "co_broker": lead.co_broker.value,
                "state": lead.state,
                "latitude": coords[0],
                "longitude": coords[1],
                "notes": lead.notes,
                "dossier_drive_link": lead.dossier_drive_link,
                "behavioral_signals": {
                    "activity_event_counts": {k.value: v for k, v in activity_counts.items()},
                    "predicted_funded_probability": float(latest_score.predicted_funded_probability) if latest_score else None,
                    "brain_mode": latest_score.mode.value if latest_score else None,
                    "call_sentiment": latest_call.sentiment if latest_call else None,
                },
            }
        )
    return results
