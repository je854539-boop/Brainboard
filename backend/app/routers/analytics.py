from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import hazard_engine

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


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
