from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import MasterLogStatus, TERMINAL_ATTRITION_STATUSES
from app.models.orm import MasterLogEntry
from app.services import floor_language, hazard_engine

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


@router.get("/renewal-queue")
def renewal_queue(limit: int = 50, db: Session = Depends(get_db)):
    """The living funding-opportunity queue, soonest first: every active
    OR FUNDED lead's next renewal/refi (on the position it already has)
    AND next stacking eligibility (2nd/3rd/... position on top of what's
    open), tagged by `queue_type` so the two stay distinguishable -- a
    deal can appear for both reasons at once. Every row also carries
    `reason_category` -- "merchant_requested" / "stacking_eligible" /
    "refi_eligible", the three ways a lead lands here. Re-derived fresh
    off current notes/balance/payment on every call, so this is the
    mechanism for catching a deal that's gone quiet but is actually
    ready, not something that has to be remembered or flagged by hand.

    Deliberately excludes TERMINAL_ATTRITION_STATUSES (Ghosted/Stalled/
    Lost To Competitor/Sold Deal Killed/Dog Shit), NOT every terminal
    status -- Funded is terminal for Kaplan-Meier purposes but is
    exactly the population this queue needs to see: refi/stacking
    opportunity is a POST-funding question. Excluding Funded here (the
    original bug) meant this queue could never see a single already-
    funded lead, which defeats its own purpose."""
    leads = db.execute(select(MasterLogEntry).where(MasterLogEntry.status.not_in(TERMINAL_ATTRITION_STATUSES))).scalars().all()
    return floor_language.renewal_queue(leads)[:limit]


@router.get("/revival-queue")
def revival_queue(min_revenue: float | None = None, limit: int = 50, db: Session = Depends(get_db)):
    """Every lead that's drifted out of the daily calendar-follow-up
    grind -- formally Stalled/Ghosted, OR still technically active but
    its follow_up_date is overdue with nothing since, OR never followed
    up at all and gone quiet -- that still looks like a real, qualified
    MCA opportunity. A separate queue from renewal-queue, built to revive
    deals that went cold (by status OR by neglect) but shouldn't have
    been given up on. Excludes Funded/Lost To Competitor/Sold Deal
    Killed/Dog Shit outright (resolved outcomes, not drift) -- see
    floor_language.revival_queue/_drift_reason for exactly how a lead
    qualifies as a candidate and the disqualifying-language/momentum
    filtering that runs after. Pass min_revenue to also require a
    minimum annual_revenue (e.g. the floor's MCA target range) -- omit
    it to skip that filter entirely."""
    leads = db.execute(select(MasterLogEntry).where(MasterLogEntry.status.not_in(floor_language.DEAD_STATUSES))).scalars().all()
    return floor_language.revival_queue(leads, min_revenue=min_revenue)[:limit]


@router.get("/hot-leads-queue")
def hot_leads_queue(limit: int = 50, db: Session = Depends(get_db)):
    """Leads still in the normal daily chase (not Stalled/Ghosted -- see
    revival-queue -- not Funded -- see renewal-queue) where recent notes
    suggest THIS WEEK is a better moment to call than whatever's already
    on the calendar. Entirely deterministic, built from the same floor-
    language signals as everywhere else -- see
    floor_language.hot_leads_queue for exactly which reasons can fire."""
    excluded = floor_language.STALLED_STATUSES | floor_language.DEAD_STATUSES
    leads = db.execute(select(MasterLogEntry).where(MasterLogEntry.status.not_in(excluded))).scalars().all()
    return floor_language.hot_leads_queue(leads)[:limit]
