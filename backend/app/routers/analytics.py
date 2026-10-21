from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import MasterLogStatus
from app.services import hazard_engine

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

_MILESTONE_PRESETS = {
    "docs": frozenset({MasterLogStatus.DOCS_IN}),
    "offer": frozenset({MasterLogStatus.OFFER_MADE_NOT_SOLD}),
    "funded": frozenset({MasterLogStatus.FUNDED}),
}


@router.get("/survival-curve")
def survival_curve(kind: Literal["attrition", "funded"] = "attrition", db: Session = Depends(get_db)):
    curve = hazard_engine.attrition_survival_curve(db) if kind == "attrition" else hazard_engine.funding_conversion_curve(db)
    return [{"t_days": p.t_days, "survival_probability": p.survival_probability} for p in curve]


@router.get("/transition-matrix")
def transition_matrix(db: Session = Depends(get_db)):
    matrix = hazard_engine.markov_transition_matrix(db)
    return {"statuses": list(matrix.index), "matrix": matrix.round(4).values.tolist()}


@router.get("/stage-velocity")
def stage_velocity(db: Session = Depends(get_db)):
    return hazard_engine.average_stage_dwell_days(db)


@router.post("/refresh")
def refresh_hazard(db: Session = Depends(get_db)):
    refreshed = hazard_engine.refresh_hazard_snapshots(db)
    return {"refreshed": refreshed}


@router.get("/cox-time-varying")
def cox_time_varying(db: Session = Depends(get_db)):
    return hazard_engine.fit_cox_time_varying(db)


@router.get("/time-to-milestone")
def time_to_milestone(
    milestone: Literal["docs", "offer", "funded"],
    group_by: Literal["co_broker", "state"] | None = None,
    db: Session = Depends(get_db),
):
    """Cumulative elapsed days from pipeline entry to first reaching the
    given milestone -- 'docs' = Docs in, 'offer' = Offer Made Not Sold,
    'funded' = Funded. Small n per group is real and expected until
    volume builds; report it, don't hide it (see n in the response)."""
    return hazard_engine.time_to_milestone(db, _MILESTONE_PRESETS[milestone], group_by=group_by)


@router.get("/outcome-rates")
def outcome_rates(group_by: Literal["co_broker", "state"] | None = None, db: Session = Depends(get_db)):
    """Ghost rate, kill rate, fund rate -- fraction of CLOSED leads landing
    in each terminal status, optionally broken out by co_broker or state."""
    return hazard_engine.outcome_rates(db, group_by=group_by)
