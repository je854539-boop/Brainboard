from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.orm import MasterLogEntry, ShadowScore
from app.services import brain

router = APIRouter(prefix="/api/brain", tags=["brain"])


def _population_status(name: str, label: str, mode, total_leads: int, threshold: int) -> dict:
    return {
        "name": name,
        "label": label,
        "mode": mode.value,
        "total_leads": total_leads,
        "threshold": threshold,
        "leads_remaining_to_live": max(0, threshold - total_leads),
    }


@router.get("/status")
def brain_status(db: Session = Depends(get_db)):
    """Top-level mode/total_leads/threshold/leads_remaining_to_live stay
    the ORGANIC population's numbers -- unchanged shape, so nothing
    already reading this endpoint breaks. `populations` is new: all
    three independently-thresholded populations (see brain.py's
    current_mode/batch_silo_mode docstrings for why they're split),
    organic listed first so it lines up with the top-level fields above
    it."""
    mode, total_leads, threshold = brain.current_mode(db)
    ucc_mode, ucc_total, ucc_threshold = brain.ucc_batch_mode(db)
    mca_mode, mca_total, mca_threshold = brain.mca_history_batch_mode(db)
    latest = db.execute(select(ShadowScore).order_by(ShadowScore.computed_at.desc()).limit(1)).scalar_one_or_none()
    return {
        "mode": mode.value,
        "total_leads": total_leads,
        "threshold": threshold,
        "leads_remaining_to_live": max(0, threshold - total_leads),
        "model_version": latest.model_version if latest else None,
        "training_set_size": latest.training_set_size if latest else 0,
        "last_refreshed_at": latest.computed_at.isoformat() if latest else None,
        "populations": [
            _population_status("organic", "Organic Master Log", mode, total_leads, threshold),
            _population_status("ucc_batch", "UCC Batch Silo", ucc_mode, ucc_total, ucc_threshold),
            _population_status("mca_history_batch", "MCA History Batch Silo", mca_mode, mca_total, mca_threshold),
        ],
    }


@router.get("/scores")
def brain_scores(limit: int = 100, db: Session = Depends(get_db)):
    """Latest ShadowScore per lead, joined with the lead's current status
    so shadow-era predictions can be visually compared to outcomes."""
    rows = db.execute(
        select(ShadowScore, MasterLogEntry)
        .join(MasterLogEntry, MasterLogEntry.lead_uid == ShadowScore.lead_uid)
        .order_by(ShadowScore.computed_at.desc())
    ).all()

    latest_by_lead = {}
    for score, lead in rows:
        if lead.lead_uid not in latest_by_lead:
            latest_by_lead[lead.lead_uid] = {
                "lead_uid": str(lead.lead_uid),
                "business_name": lead.business_name,
                "status": lead.status.value,
                "predicted_funded_probability": float(score.predicted_funded_probability),
                "model_version": score.model_version,
                "mode": score.mode.value,
                "computed_at": score.computed_at.isoformat(),
            }
    return list(latest_by_lead.values())[:limit]


@router.post("/refresh")
def refresh_brain(db: Session = Depends(get_db)):
    scored = brain.refresh_shadow_scores(db)
    return {"scored": scored}
