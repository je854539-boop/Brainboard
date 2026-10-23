"""The Brain: a transparent, real (not simulated) logistic-regression
scoring engine that predicts funded-probability for every lead.

Runs in SHADOW mode -- predictions are logged to ShadowScore for every
lead on every refresh, but never surfaced as an actionable
recommendation -- until the pipeline has accumulated
`BRAIN_SHADOW_MODE_LEAD_THRESHOLD` (default 1000) leads, at which point
mode flips to LIVE. Shadow-era predictions stay in the ledger either way,
so once a shadow-era lead resolves you can back-test how the model would
have called it.

This is a small, explainable model on purpose, but it's grown real ears
since the summary above was first written: numeric deal terms, one-hot
co-broker and source-silo, an activity-count engagement signal, real
call sentiment/volume off every Deepgram-analyzed recording (the Brain
genuinely listening to calls, not a metaphor), deterministic lexicon
signals pulled from the lead's own notes (floor_language.py), and --
one-hot per marker -- whichever of silo_leadgen.py's own real
corroboration markers (TARIFF-AFFECTED SOURCING, ASIA SOURCING
CONFIRMED, LARGEST VOLUME IMPORTER, etc.) rode along on a converted
candidate's notes into this lead. All of it goes into scikit-learn's
LogisticRegression on whatever terminal (Funded / attrited) leads exist
so far. With few labeled examples the fit will be weak -- that's
reported honestly via `training_set_size`, not hidden.
"""

import asyncio
import logging

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import BrainMode, CallAnalysisStatus, CoBroker, MasterLogStatus, SiloName, TERMINAL_ATTRITION_STATUSES, TERMINAL_STATUSES
from app.models.orm import CallRecording, LeadActivityEvent, MasterLogEntry, ShadowScore
from app.services import floor_language
from app.services.silo_leadgen import _SIGNAL_PERFORMANCE_MARKERS

logger = logging.getLogger("brainboard.brain")

MODEL_VERSION = "logreg-v1"
BASELINE_VERSION = "baseline-insufficient-data"
MIN_TRAINING_EXAMPLES = 10  # need both classes represented and a minimum sample before trusting a real fit

NUMERIC_FEATURES = ["annual_revenue", "credit_score", "open_positions", "current_balance", "payment_amt"]


def _call_signal(payload: dict) -> float:
    """Deepgram's sentiment block carries an {"average": {"sentiment_score": ...}}
    summary alongside per-segment detail -- this pulls just that single
    number (-1..1, negative to positive) per call, defaulting to neutral
    (0.0) if a recording has no sentiment yet (e.g. still PENDING/FAILED)."""
    sentiment = payload or {}
    average = sentiment.get("average") if isinstance(sentiment, dict) else None
    score = average.get("sentiment_score") if isinstance(average, dict) else None
    return float(score) if isinstance(score, (int, float)) else 0.0


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

    # Every call the dialer auto-analyzes (see services/dialer.py::
    # auto_analyze_recording) lands here -- this is the actual mechanism
    # behind "the Brain observes calls": call volume and tone become
    # real, learned features, not just metadata logged somewhere nobody
    # looks at. Leads with no analyzed call yet get 0.0 on both --
    # correct for "hasn't been called/no signal yet", not a data gap.
    call_rows = db.execute(
        select(CallRecording.lead_uid, CallRecording.sentiment)
        .where(CallRecording.lead_uid.is_not(None), CallRecording.status == CallAnalysisStatus.COMPLETED)
    ).all()
    call_counts: dict = {}
    call_sentiment_sums: dict = {}
    for lead_uid, sentiment in call_rows:
        call_counts[lead_uid] = call_counts.get(lead_uid, 0) + 1
        call_sentiment_sums[lead_uid] = call_sentiment_sums.get(lead_uid, 0.0) + _call_signal(sentiment)

    rows = []
    for lead in leads:
        row = {f: (float(getattr(lead, f)) if getattr(lead, f) is not None else 0.0) for f in NUMERIC_FEATURES}
        row["activity_count"] = float(activity_counts.get(lead.lead_uid, 0))
        analyzed_calls = call_counts.get(lead.lead_uid, 0)
        row["analyzed_call_count"] = float(analyzed_calls)
        row["avg_call_sentiment"] = (
            call_sentiment_sums.get(lead.lead_uid, 0.0) / analyzed_calls if analyzed_calls else 0.0
        )
        # The "floor intelligence" track -- deterministic lexicon
        # extraction over the lead's own notes (services/floor_language.py),
        # not a third-party call. This is information no enrichment API
        # can ever see (deal momentum, stacking, PIF/renewal signal,
        # factor rates actually quoted), complementing rather than
        # replacing the verified-facts track above.
        floor_signals = floor_language.extract_signals(lead.business_name, lead.notes)
        row["floor_momentum_score"] = float(floor_signals["momentum_score"])
        row["floor_recent_payoff_detected"] = 1.0 if floor_signals["recent_payoff_detected"] else 0.0
        row["floor_stacking_count"] = float(floor_signals["stacking_count_mentioned"] or 0)
        row["floor_mentioned_lender_count"] = float(len(floor_signals["mentioned_lenders"]))
        # The trajectory view -- is this specific deal warming up or
        # cooling off, not just "what does the whole note blob say". See
        # floor_language.extract_timeline's docstring for why this is a
        # materially different (and more predictive) signal than the
        # aggregate momentum score above.
        timeline = floor_language.extract_timeline(lead.notes)
        row["floor_momentum_trend"] = float(timeline["momentum_trend"])
        row["floor_touch_count"] = float(timeline["entry_count"])
        row["floor_days_since_last_touch"] = float(timeline["days_since_last_entry"] or 0)
        # How many distinct brokers have actually worked this lead (not
        # just the one assigned co_broker) and how many hand-offs it's
        # been through -- a deal passed around a lot is a different
        # risk/effort profile than one worked by a single owner the whole
        # way. balance_trend/new_advance_detected are the closest this
        # lexicon gets to "is this merchant actually approaching a
        # renewal or just getting more stacked" without a real API call.
        row["floor_people_touched_count"] = float(timeline["touch_count_people"])
        row["floor_handoff_count"] = float(len(timeline["handoff_chain"]))
        row["floor_balance_decreasing"] = 1.0 if timeline["balance_trend"] == "decreasing" else 0.0
        row["floor_new_advance_detected"] = 1.0 if timeline["new_advance_detected"] else 0.0
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
        # The real "listening to the brain's own reasoning" track --
        # convert_or_update_silo_candidate (pipeline.py) copies a
        # converted candidate's full notes, markers and all, verbatim
        # into the new lead's own notes field. That means every one of
        # silo_leadgen.py's real corroboration markers (TARIFF-AFFECTED
        # SOURCING, ASIA SOURCING CONFIRMED, LARGEST VOLUME IMPORTER,
        # etc.) is already sitting right there in lead.notes -- this is
        # what finally lets the model learn whether a tariff-driven lead
        # funds differently than a volume-ranked one, controlling for
        # deal terms and call sentiment, instead of treating every
        # silo-sourced lead as interchangeable. Same one-hot-per-
        # category shape as source_silo/co_broker above, and the same
        # single source of truth _signal_performance_report already
        # uses, not a second, separately-maintained marker list.
        notes_text = lead.notes or ""
        for marker in _SIGNAL_PERFORMANCE_MARKERS:
            slug = "".join(c if c.isalnum() else "_" for c in marker.lower())
            row[f"marker__{slug}"] = 1.0 if marker in notes_text else 0.0
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


# ---------------------------------------------------------------------------
# Background refresh -- must not block the FastAPI event loop
# ---------------------------------------------------------------------------

_refresh_task: asyncio.Task | None = None


async def _refresh_loop() -> None:
    from app.database import SessionLocal

    while True:
        try:
            db = SessionLocal()
            try:
                scored = await asyncio.to_thread(refresh_shadow_scores, db)
                logger.info("Brain shadow-score refresh complete: %d leads scored", scored)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 -- one bad cycle must not kill the loop
            logger.exception("Brain shadow-score refresh failed -- will retry next cycle")
        await asyncio.sleep(get_settings().brain_shadow_refresh_interval_seconds)


def start_background_refresh() -> asyncio.Task | None:
    """Keeps ShadowScore current automatically instead of relying on
    someone remembering to click "Recompute Scores" on /brain -- on by
    default (brain_continuous_refresh_enabled) since this is pure local
    DB computation, not an external API integration that needs an
    explicit opt-in the way river surveillance does."""
    global _refresh_task
    if not get_settings().brain_continuous_refresh_enabled:
        logger.info("Brain continuous refresh disabled (set BRAIN_CONTINUOUS_REFRESH_ENABLED=false to restore manual-only)")
        return None
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(_refresh_loop())
        logger.info("Brain background refresh started (interval=%ss)", get_settings().brain_shadow_refresh_interval_seconds)
    return _refresh_task


def stop_background_refresh() -> None:
    global _refresh_task
    if _refresh_task is not None:
        _refresh_task.cancel()
        _refresh_task = None
        logger.info("Brain background refresh stopped")
