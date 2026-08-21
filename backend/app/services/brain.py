"""The Brain: a transparent, real (not simulated) logistic-regression
scoring engine that predicts funded-probability for every lead.

Runs in SHADOW mode -- predictions are logged to ShadowScore for every
lead on every refresh, but never surfaced as an actionable
recommendation -- until the pipeline has accumulated
`BRAIN_SHADOW_MODE_LEAD_THRESHOLD` (default 1000) leads, at which point
mode flips to LIVE. Shadow-era predictions stay in the ledger either way,
so once a shadow-era lead resolves you can back-test how the model would
have called it.

This is a small, explainable model on purpose: a handful of numeric deal
terms, one-hot co-broker, and an activity-count engagement signal, fit
with scikit-learn's LogisticRegression on whatever terminal (Funded /
attrited) leads exist so far. With few labeled examples the fit will be
weak -- that's reported honestly via `training_set_size`, not hidden.
"""

import logging

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import BrainMode, CoBroker, MasterLogStatus, SiloName, TERMINAL_ATTRITION_STATUSES, TERMINAL_STATUSES
from app.models.orm import LeadActivityEvent, MasterLogEntry, ShadowScore

logger = logging.getLogger("brainboard.brain")

MODEL_VERSION = "logreg-v1"
BASELINE_VERSION = "baseline-insufficient-data"
MIN_TRAINING_EXAMPLES = 10  # need both classes represented and a minimum sample before trusting a real fit

NUMERIC_FEATURES = ["annual_revenue", "credit_score", "open_positions", "current_balance", "payment_amt"]


def current_mode(db: Session) -> tuple[BrainMode, int, int]:
    settings = get_settings()
    total_leads = db.execute(select(func.count()).select_from(MasterLogEntry)).scalar_one()
    threshold = settings.brain_shadow_mode_lead_threshold
    mode = BrainMode.LIVE if total_leads >= threshold else BrainMode.SHADOW
    return mode, total_leads, threshold


def _lead_features(db: Session, leads: list[MasterLogEntry]) -> pd.DataFrame:
    activity_counts = dict(
        db.execute(
            select(LeadActivityEvent.lead_uid, func.count()).group_by(LeadActivityEvent.lead_uid)
        ).all()
    )

    rows = []
    for lead in leads:
        row = {f: (float(getattr(lead, f)) if getattr(lead, f) is not None else 0.0) for f in NUMERIC_FEATURES}
        row["activity_count"] = float(activity_counts.get(lead.lead_uid, 0))
        for cb in CoBroker:
            row[f"co_broker__{cb.value}"] = 1.0 if lead.co_broker == cb else 0.0
        # source_silo is NULL for a lead entered directly (intake form,
        # hand-typed Sheet row) -- leaving every indicator at 0.0 is the
        # correct one-hot representation of "not silo-sourced", not a
        # missing-data gap, so no explicit "none" column is needed. This
        # is what lets the model learn whether silo-sourced leads
        # fund/attrite differently than manually-entered ones, and which
        # silo specifically -- the actual mechanism for "smarter lead
        # picking" once enough labeled examples exist.
        for silo in SiloName:
            row[f"source_silo__{silo.value}"] = 1.0 if lead.source_silo == silo else 0.0
        rows.append(row)
    return pd.DataFrame(rows, index=[lead.lead_uid for lead in leads])


def _fit_model(db: Session) -> tuple[LogisticRegression | None, StandardScaler | None, int]:
    terminal_leads = db.execute(select(MasterLogEntry).where(MasterLogEntry.status.in_(TERMINAL_STATUSES))).scalars().all()
    if len(terminal_leads) < MIN_TRAINING_EXAMPLES:
        return None, None, len(terminal_leads)

    labels = np.array([1 if lead.status == MasterLogStatus.FUNDED else 0 for lead in terminal_leads])
    if labels.sum() == 0 or labels.sum() == len(labels):
        return None, None, len(terminal_leads)  # need both outcomes represented to fit a real classifier

    features = _lead_features(db, terminal_leads)
    scaler = StandardScaler()
    scaled = scaler.fit_transform(features.values)

    model = LogisticRegression(max_iter=1000)
    model.fit(scaled, labels)
    return model, scaler, len(terminal_leads)


def refresh_shadow_scores(db: Session) -> int:
    mode, total_leads, _ = current_mode(db)
    model, scaler, training_set_size = _fit_model(db)
    model_version = MODEL_VERSION if model is not None else BASELINE_VERSION

    baseline_rate = 0.5
    if model is None and training_set_size > 0:
        funded_count = db.execute(
            select(func.count()).select_from(MasterLogEntry).where(MasterLogEntry.status == MasterLogStatus.FUNDED)
        ).scalar_one()
        terminal_count = db.execute(
            select(func.count()).select_from(MasterLogEntry).where(MasterLogEntry.status.in_(TERMINAL_STATUSES))
        ).scalar_one()
        baseline_rate = funded_count / terminal_count if terminal_count else 0.5

    all_leads = db.execute(select(MasterLogEntry)).scalars().all()
    if not all_leads:
        return 0

    features = _lead_features(db, all_leads)
    if model is not None:
        scaled = scaler.transform(features.values)
        probabilities = model.predict_proba(scaled)[:, 1]
    else:
        probabilities = [baseline_rate] * len(all_leads)

    for lead, probability in zip(all_leads, probabilities):
        db.add(
            ShadowScore(
                lead_uid=lead.lead_uid,
                predicted_funded_probability=round(float(probability), 4),
                model_version=model_version,
                mode=mode,
                training_set_size=training_set_size,
            )
        )
    db.commit()
    return len(all_leads)
