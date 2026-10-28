"""The MCA History Batch Silo pipeline -- ingestion, dedupe, and
Hot/Medium/Cold tiering for the 18,000-contact purchased list: existing
businesses with a current or prior MCA position and $100K/mo+ revenue.

Same staging posture as services/ucc_batch.py and the same reason for it:
a raw contact record has no call history yet, so it lands as a
SiloCandidate (silo=MCA_HISTORY_BATCH_SILO, status=PENDING) -- staged,
workable off the dialer, outside brain.py's shadow-mode count until a
real human touchpoint promotes it via the existing pipeline.
convert_or_update_silo_candidate, same conversion path every other silo
already uses.

Kept as its own module rather than folded into ucc_batch.py because the
real signal here is different in kind, not just in source: current/prior
MCA position (proven payment behavior with a prior or existing funder) is
the primary tiering input, not a logistics-keyword/FMCSA angle, and the
revenue floor is this list's own ($100K/mo+ / $1.2M/yr+), explicitly NOT
the $5M-$49M band the UCC batch and CME Macro Funnel use.
"""

import csv
import io
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import SiloCandidateStatus, SiloName, TelemetrySource
from app.models.orm import MasterLogEntry, SiloCandidate
from app.services import entity_matching

logger = logging.getLogger("brainboard.mca_history_batch")

MCA_HISTORY_BATCH_SILO = SiloName.MCA_HISTORY_BATCH_SILO

REQUIRED_CSV_COLUMNS = {"business_name"}
OPTIONAL_CSV_COLUMNS = {
    "contact_name", "phone", "email", "state",
    "monthly_revenue", "annual_revenue",
    "mca_status", "current_funder", "current_balance", "position_count",
}


def _parse_float(value: str | None) -> float | None:
    if not value or not value.strip():
        return None
    try:
        return float(value.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _annual_revenue(row: dict) -> float | None:
    annual = _parse_float(row.get("annual_revenue"))
    if annual is not None:
        return annual
    monthly = _parse_float(row.get("monthly_revenue"))
    return monthly * 12 if monthly is not None else None


def score_candidate(row: dict) -> tuple[float, bool, list[str]]:
    """0-100 tiering score plus a hard-dismiss flag and the notes
    explaining why. Only the FLOOR is enforced here (this list's own
    $100K/mo+, settings.mca_history_batch_revenue_min_usd) -- unlike
    ucc_batch.py's band check, no ceiling was given for this list, so a
    confirmed-large account is never dismissed for being too big. Never
    dismisses for lack of data -- only a confirmed revenue figure below
    the floor does. Reasoned weights, not measured -- current/prior MCA
    position outweighs revenue and contact completeness combined,
    because proven payment behavior with a real funder is a stronger buy
    signal than either on its own."""
    settings = get_settings()
    score = 10.0
    notes: list[str] = []
    dismiss = False

    if row.get("phone"):
        score += 20.0
        notes.append("phone on file")
    if row.get("email"):
        score += 15.0
        notes.append("email on file")

    revenue = _annual_revenue(row)
    if revenue is not None:
        floor = settings.mca_history_batch_revenue_min_usd
        if revenue >= floor:
            score += 15.0
            notes.append(f"revenue ${revenue:,.0f}/yr clears the ${floor:,.0f}/yr floor")
        else:
            score -= 30.0
            dismiss = True
            notes.append(f"revenue ${revenue:,.0f}/yr below the ${floor:,.0f}/yr floor, dismissed")

    mca_status = (row.get("mca_status") or "").strip().lower()
    if mca_status == "current":
        score += 25.0
        notes.append("currently in an MCA position -- proven payment behavior, real refinance/stacking candidate")
    elif mca_status == "prior":
        score += 15.0
        notes.append("prior MCA position on file -- proven willingness, no current obligation blocking a new one")

    position_count = row.get("position_count")
    try:
        if position_count is not None and int(position_count) >= 2:
            score += 10.0
            notes.append(f"{position_count} prior/current positions -- stacking-experienced")
    except (TypeError, ValueError):
        pass

    if row.get("current_funder"):
        notes.append(f"current funder on file: {row['current_funder']}")

    return max(0.0, min(100.0, score)), dismiss, notes


def tier_for_score(score: float) -> str:
    settings = get_settings()
    if score >= settings.mca_history_batch_hot_score_threshold:
        return "hot"
    if score >= settings.mca_history_batch_medium_score_threshold:
        return "medium"
    return "cold"


def _duplicate_of_existing_lead(db: Session, business_name: str, api_key: str) -> bool:
    """Same Interzoid-fuzzy-or-substring-fallback dedupe as
    ucc_batch.py's matching function -- avoids re-pitching a merchant
    already in Master Log V2. O(n) per candidate; fine at hundreds of
    rows, revisit (a materialized name index) if the book itself grows
    into the thousands."""
    existing_names = db.execute(select(MasterLogEntry.business_name)).scalars().all()
    return any(entity_matching.names_match(api_key, business_name, existing) for existing in existing_names)


def import_mca_history_batch_csv(db: Session, file_content: bytes) -> dict:
    """Parses the uploaded MCA history batch CSV into SiloCandidate rows
    (silo=MCA_HISTORY_BATCH_SILO, status=PENDING) -- staged, not counted
    toward brain.py's threshold until a caller promotes one off the
    dialer. Same "import, then the dialer does the rest" shape as
    ucc_batch.import_ucc_batch_csv -- no separate enrichment sweep here,
    since this list's own fields (mca_status, revenue) are already the
    whole signal, unlike the UCC batch's FMCSA fleet-check follow-up."""
    text = file_content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None or not REQUIRED_CSV_COLUMNS.issubset(set(reader.fieldnames)):
        missing = REQUIRED_CSV_COLUMNS - set(reader.fieldnames or [])
        return {"error": f"CSV missing required column(s): {', '.join(sorted(missing))}", "imported": 0}

    settings = get_settings()
    imported = 0
    duplicates = 0
    dismissed = 0
    tier_counts = {"hot": 0, "medium": 0, "cold": 0}
    seen_in_batch: set[str] = set()

    for i, row in enumerate(reader):
        business_name = (row.get("business_name") or "").strip()
        if not business_name:
            continue
        name_key = business_name.lower()
        if name_key in seen_in_batch:
            duplicates += 1
            continue
        if _duplicate_of_existing_lead(db, business_name, settings.interzoid_api_key):
            duplicates += 1
            continue
        seen_in_batch.add(name_key)

        score, dismiss, score_notes = score_candidate(row)
        tier = tier_for_score(score)
        tier_counts[tier] += 1
        status = SiloCandidateStatus.DISMISSED if dismiss else SiloCandidateStatus.PENDING
        if dismiss:
            dismissed += 1

        notes_lines = [f"MCA history batch import, row {i + 1}.", f"Tier: {tier.upper()} (score {score:.0f})."]
        notes_lines.extend(score_notes)

        db.add(
            SiloCandidate(
                silo=MCA_HISTORY_BATCH_SILO,
                company_name=business_name,
                contact_name=row.get("contact_name") or None,
                phone=row.get("phone") or None,
                email=row.get("email") or None,
                source_reference=f"mca_history_batch_row:{i + 1} [mca_history_batch_upload]",
                notes="\n".join(notes_lines),
                score=score,
                status=status,
                origin_source=TelemetrySource.MCA_HISTORY_BATCH_UPLOAD,
            )
        )
        imported += 1

    db.commit()
    return {
        "imported": imported,
        "duplicates_skipped": duplicates,
        "dismissed_on_revenue_floor": dismissed,
        "tier_counts": tier_counts,
    }
