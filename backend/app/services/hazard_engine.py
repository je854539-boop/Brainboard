"""Kaplan-Meier survival analysis + empirical Markov transition modeling
over the Master Log pipeline.

Two complementary lenses on the same StatusHistory ledger:

  * Kaplan-Meier: for a lead of age t (days since entering the pipeline),
    what is the probability it survives (has not attrited) past t? A
    second curve tracks time-to-Funded the same way.
  * Markov chain: empirical stage-to-stage transition probabilities,
    estimated from every observed (from_status -> to_status) edge in
    StatusHistory. Used both for the Analytics deck's transition matrix
    and to derive a per-lead "probability of moving off current stage".
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
from lifelines import CoxTimeVaryingFitter, KaplanMeierFitter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import TERMINAL_ATTRITION_STATUSES, TERMINAL_STATUSES, ActivityEventType, MasterLogStatus
from app.models.orm import HazardSnapshot, LeadActivityEvent, MasterLogEntry, StatusHistory

ALL_STATUSES: list[MasterLogStatus] = list(MasterLogStatus)


@dataclass
class SurvivalPoint:
    t_days: float
    survival_probability: float


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lead_timelines(db: Session) -> pd.DataFrame:
    """One row per lead: entered_at, last_event_at, terminal_status (or
    None if still active)."""
    leads = db.execute(select(MasterLogEntry)).scalars().all()
    rows = []
    for lead in leads:
        history = sorted(lead.status_history, key=lambda h: h.changed_at)
        entered_at = history[0].changed_at if history else lead.created_at
        last_event_at = history[-1].changed_at if history else lead.created_at
        terminal_status = lead.status if lead.status in TERMINAL_STATUSES else None
        rows.append(
            {
                "lead_uid": str(lead.lead_uid),
                "entered_at": entered_at,
                "last_event_at": last_event_at,
                "current_status": lead.status,
                "terminal_status": terminal_status,
            }
        )
    return pd.DataFrame(rows)


def _duration_days(row) -> float:
    end = row["last_event_at"] if row["terminal_status"] is not None else _now()
    delta = end - row["entered_at"]
    return max(delta.total_seconds() / 86400.0, 0.0)


def fit_survival_curve(db: Session, *, event_statuses: frozenset[MasterLogStatus]) -> list[SurvivalPoint]:
    """Fit a Kaplan-Meier curve where the "event" is reaching any status in
    `event_statuses`. Leads that are still active, or that terminated in a
    different terminal bucket, are right-censored."""
    df = _lead_timelines(db)
    if df.empty:
        return []

    df["duration"] = df.apply(_duration_days, axis=1)
    df["event_observed"] = df["terminal_status"].apply(lambda s: bool(s in event_statuses))

    kmf = KaplanMeierFitter()
    kmf.fit(durations=df["duration"], event_observed=df["event_observed"])

    survival_function = kmf.survival_function_.reset_index()
    survival_function.columns = ["t_days", "survival_probability"]
    return [
        SurvivalPoint(t_days=float(r["t_days"]), survival_probability=float(r["survival_probability"]))
        for _, r in survival_function.iterrows()
    ]


def attrition_survival_curve(db: Session) -> list[SurvivalPoint]:
    """S(t): probability a lead has NOT attrited (Ghosted / Loss to
    Competitor / Dog Shit / Sold Deal Killed / Deal Stalled Proxy Pass) by
    day t. Funded leads are treated as censored (they didn't die)."""
    return fit_survival_curve(db, event_statuses=TERMINAL_ATTRITION_STATUSES)


def funding_conversion_curve(db: Session) -> list[SurvivalPoint]:
    """S(t) here is read inversely: 1 - S(t) is the cumulative probability
    of having reached Funded by day t."""
    return fit_survival_curve(db, event_statuses=frozenset({MasterLogStatus.FUNDED}))


def markov_transition_matrix(db: Session) -> pd.DataFrame:
    """Empirical stage-to-stage transition matrix P[i][j] = probability of
    moving to status j given the lead is currently in status i, estimated
    from observed StatusHistory edges. Rows with no observed transitions
    are left as an identity (self-loop probability 1.0), i.e. "no data yet
    -> assume it stays put"."""
    edges = db.execute(select(StatusHistory).where(StatusHistory.from_status.is_not(None))).scalars().all()

    matrix = pd.DataFrame(0.0, index=[s.value for s in ALL_STATUSES], columns=[s.value for s in ALL_STATUSES])
    for edge in edges:
        matrix.loc[edge.from_status.value, edge.to_status.value] += 1

    row_sums = matrix.sum(axis=1)
    for status_value in matrix.index:
        total = row_sums[status_value]
        if total == 0:
            matrix.loc[status_value, status_value] = 1.0
        else:
            matrix.loc[status_value] = matrix.loc[status_value] / total
    return matrix


def _nearest_survival_probability(curve: list[SurvivalPoint], t_days: float) -> float:
    if not curve:
        return 1.0
    candidates = [p for p in curve if p.t_days <= t_days]
    return (candidates[-1] if candidates else curve[0]).survival_probability


def average_stage_dwell_days(db: Session) -> dict[str, float]:
    """Mean days spent in each status before transitioning out of it,
    computed from consecutive StatusHistory entries per lead. Surfaces
    deal-velocity bottlenecks on the Analytics & Hazard Deck."""
    history = db.execute(select(StatusHistory).order_by(StatusHistory.lead_uid, StatusHistory.changed_at)).scalars().all()

    dwell_by_status: dict[str, list[float]] = {s.value: [] for s in ALL_STATUSES}
    by_lead: dict[str, list[StatusHistory]] = {}
    for entry in history:
        by_lead.setdefault(str(entry.lead_uid), []).append(entry)

    for entries in by_lead.values():
        for current, nxt in zip(entries, entries[1:]):
            days = (nxt.changed_at - current.changed_at).total_seconds() / 86400.0
            dwell_by_status[current.to_status.value].append(max(days, 0.0))

    return {status: (sum(days) / len(days) if days else 0.0) for status, days in dwell_by_status.items()}


def refresh_hazard_snapshots(db: Session) -> int:
    """Recompute survival + transition-off-stage probability for every
    active (non-terminal) lead and upsert HazardSnapshot rows. Returns the
    number of leads refreshed."""
    attrition_curve = attrition_survival_curve(db)
    transition_matrix = markov_transition_matrix(db)

    leads = db.execute(select(MasterLogEntry).where(MasterLogEntry.status.not_in(TERMINAL_STATUSES))).scalars().all()

    refreshed = 0
    for lead in leads:
        history = sorted(lead.status_history, key=lambda h: h.changed_at)
        stage_entered_at = history[-1].changed_at if history else lead.created_at
        pipeline_entered_at = history[0].changed_at if history else lead.created_at

        days_in_stage = max((_now() - stage_entered_at).total_seconds() / 86400.0, 0.0)
        days_in_pipeline = max((_now() - pipeline_entered_at).total_seconds() / 86400.0, 0.0)

        survival_probability = _nearest_survival_probability(attrition_curve, days_in_pipeline)

        row = transition_matrix.loc[lead.status.value]
        self_loop = row.get(lead.status.value, 0.0)
        next_stage_transition_probability = max(0.0, min(1.0, 1.0 - float(self_loop)))

        snapshot = db.get(HazardSnapshot, lead.lead_uid)
        if snapshot is None:
            snapshot = HazardSnapshot(lead_uid=lead.lead_uid)
            db.add(snapshot)
        snapshot.survival_probability = round(survival_probability, 4)
        snapshot.days_in_stage = int(days_in_stage)
        snapshot.next_stage_transition_probability = round(next_stage_transition_probability, 4)
        refreshed += 1

    db.commit()
    return refreshed


def fit_cox_time_varying(db: Session) -> dict:
    """Fits a Cox proportional-hazards model with time-varying covariates
    over the LeadActivityEvent ledger -- the same append-only log that
    "every click, every calendar change, every note change" writes to.
    Complements the static Kaplan-Meier curve above by letting engagement
    intensity itself modulate funded-hazard over a lead's lifetime, not
    just elapsed time. A hazard ratio > 1 for a covariate means more of
    that activity is associated with a HIGHER instantaneous chance of
    funding at that moment; < 1 means the opposite.

    Reports "insufficient data" rather than raising when there aren't
    enough resolved leads with recorded activity to fit reliably --
    small pipelines will see this until enough leads accumulate.
    """
    leads = db.execute(select(MasterLogEntry)).scalars().all()
    activity = db.execute(
        select(LeadActivityEvent).order_by(LeadActivityEvent.lead_uid, LeadActivityEvent.occurred_at)
    ).scalars().all()

    activity_by_lead: dict[object, list[LeadActivityEvent]] = {}
    for event in activity:
        activity_by_lead.setdefault(event.lead_uid, []).append(event)

    episodes = []
    for lead in leads:
        events = activity_by_lead.get(lead.lead_uid, [])
        t0 = lead.created_at
        is_funded = lead.status == MasterLogStatus.FUNDED
        stop_at = lead.updated_at if lead.status in TERMINAL_STATUSES else _now()
        if stop_at <= t0:
            continue

        breakpoints = sorted({e.occurred_at for e in events if t0 < e.occurred_at < stop_at})
        boundaries = [t0] + breakpoints + [stop_at]

        # Two boundaries that differ by a fraction of a second are distinct
        # in raw float terms but can round to the same day value, which
        # lifelines' CoxTimeVaryingFitter rejects as a zero-length episode
        # (start == stop). Rather than just dropping the degenerate episode
        # (which would silently discard the terminal funded/attrited event
        # whenever it lands on the same rounded day as the prior
        # breakpoint), collapse same-day boundaries into one, keeping the
        # later timestamp so stop_at's identity survives the merge.
        merged_boundaries = [boundaries[0]]
        merged_days = [0.0]
        for b in boundaries[1:]:
            day = round((b - t0).total_seconds() / 86400.0, 4)
            if day == merged_days[-1]:
                merged_boundaries[-1] = b  # later timestamp wins, so stop_at is never the one dropped
                merged_days[-1] = day
            else:
                merged_boundaries.append(b)
                merged_days.append(day)
        boundaries = merged_boundaries

        total_count = note_count = calendar_count = 0
        idx = 0
        for start, stop in zip(boundaries, boundaries[1:]):
            start_day = round((start - t0).total_seconds() / 86400.0, 4)
            stop_day = round((stop - t0).total_seconds() / 86400.0, 4)
            while idx < len(events) and events[idx].occurred_at <= start:
                total_count += 1
                if events[idx].event_type == ActivityEventType.NOTE_CHANGE:
                    note_count += 1
                if events[idx].event_type == ActivityEventType.CALENDAR_SYNC:
                    calendar_count += 1
                idx += 1
            episodes.append(
                {
                    "id": str(lead.lead_uid),
                    "start": start_day,
                    "stop": stop_day,
                    "event": 1 if (stop == stop_at and is_funded) else 0,
                    "activity_count": total_count,
                    "note_changes": note_count,
                    "calendar_syncs": calendar_count,
                }
            )

    n_events = sum(e["event"] for e in episodes)
    if len(episodes) < 20 or n_events < 5:
        return {
            "fitted": False,
            "reason": f"insufficient data ({len(episodes)} episodes, {n_events} funded outcomes -- need >=20 episodes and >=5 outcomes)",
            "n_episodes": len(episodes),
            "n_leads": len(leads),
        }

    df = pd.DataFrame(episodes)
    try:
        ctv = CoxTimeVaryingFitter()
        ctv.fit(df, id_col="id", start_col="start", stop_col="stop", event_col="event")
    except Exception as exc:  # noqa: BLE001 -- a non-convergent fit must degrade gracefully, not 500 the dashboard
        return {
            "fitted": False,
            "reason": f"model did not converge: {exc}",
            "n_episodes": len(episodes),
            "n_leads": len(leads),
        }

    return {
        "fitted": True,
        "n_episodes": len(episodes),
        "n_leads": len(leads),
        "n_events": int(n_events),
        "hazard_ratios": {cov: round(float(hr), 4) for cov, hr in ctv.hazard_ratios_.items()},
    }
