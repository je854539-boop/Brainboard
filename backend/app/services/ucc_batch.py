"""The one-time bulk UCC-lien batch pipeline -- ingestion, dedupe,
Hot/Medium/Cold tiering, and FMCSA enrichment for the UCC Batch Silo.

This is deliberately NOT the same path as
enrichment_orchestrator.import_leads_csv, which creates MasterLogEntry
rows directly (the counted population). A raw UCC record has no call
history, no deal terms, no activity -- an empty feature vector for
brain.py. Pushing 20,000 of those straight into Master Log V2 just to
clear the Brain's lead-count threshold faster would pollute its
calibration with signal-free rows, not help it. So this batch lands as
SiloCandidate rows (silo=UCC_BATCH_SILO, status=PENDING) instead --
staged, visible, workable off the dialer, but outside brain.py's count
until a real human touchpoint exists. The existing conversion path
(pipeline.convert_or_update_silo_candidate, same one every other silo
already uses) is what promotes a candidate into a real, counted
MasterLogEntry -- unchanged, reused as-is, the moment a caller advances
one off the dialer.

The source data itself is an enriched UCC-lead product, not a bare lien
filing -- most rows already carry a contact name, phone, email, and a
revenue range (confirmed by the user directly, correcting an earlier
assumption that Apollo-style contact-attach enrichment would be needed
here). What this module actually adds on top of the source data is:
dedupe against the existing book, a logistics-keyword read for the
Fuel-Cost UCC Funnel / FMCSA fleet-check path, and a 0-100 tiering score
so Hot/Medium candidates can be worked first.
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
from app.services.enrichment.base import EnrichmentQuery
from app.services.enrichment.fmcsa import FMCSAAdapter
from app.services.industry_classifier import guess_industry_tag

logger = logging.getLogger("brainboard.ucc_batch")

UCC_BATCH_SILO = SiloName.UCC_BATCH_SILO

REQUIRED_CSV_COLUMNS = {"business_name"}
OPTIONAL_CSV_COLUMNS = {
    "contact_name", "phone", "email", "state",
    "revenue_range_min", "revenue_range_max",
    "collateral_description", "filing_date", "secured_party",
}

# Same keyword set discussed for the Fuel-Cost UCC Funnel's Identify
# stage -- a plausible trucking/logistics fit by name or collateral
# description, worth a free FMCSA fleet-size check. Deliberately broad
# (real false-positive risk on generic LLC names) rather than narrow --
# a wasted free FMCSA lookup costs nothing; a missed real logistics
# company costs a lead.
LOGISTICS_KEYWORDS = (
    "trucking", "logistics", "freight", "transport", "transportation",
    "hauling", "carrier", "carriers", "drayage", "fleet", "delivery",
    "distribution", "cartage", "shipping",
)


def _is_logistics_flavored(business_name: str, collateral_description: str | None) -> bool:
    haystack = f"{business_name} {collateral_description or ''}".lower()
    return any(keyword in haystack for keyword in LOGISTICS_KEYWORDS)


def _parse_float(value: str | None) -> float | None:
    if not value or not value.strip():
        return None
    try:
        return float(value.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def score_candidate(row: dict) -> tuple[float, bool, list[str]]:
    """0-100 tiering score plus a hard-dismiss flag and the notes
    explaining why. Never dismisses for lack of data -- only a CONFIRMED
    revenue figure outside settings.mca_target_revenue_min/max_usd does,
    same honesty posture as silo_leadgen.py's own revenue-range filter.
    Everything else is additive, reasoned, not measured -- revisit once
    real call outcomes exist to validate the bands against."""
    settings = get_settings()
    score = 10.0  # baseline: the row exists and passed dedupe
    notes: list[str] = []
    dismiss = False

    business_name = row.get("business_name", "")
    collateral = row.get("collateral_description")

    if row.get("phone"):
        score += 20.0
        notes.append("phone on file")
    if row.get("email"):
        score += 15.0
        notes.append("email on file")

    revenue_min = _parse_float(row.get("revenue_range_min"))
    revenue_max = _parse_float(row.get("revenue_range_max"))
    if revenue_min is not None or revenue_max is not None:
        band_low, band_high = settings.mca_target_revenue_min_usd, settings.mca_target_revenue_max_usd
        candidate_low = revenue_min if revenue_min is not None else revenue_max
        candidate_high = revenue_max if revenue_max is not None else revenue_min
        overlaps_band = candidate_low <= band_high and candidate_high >= band_low
        if overlaps_band:
            score += 20.0
            notes.append(f"revenue range ${candidate_low:,.0f}-${candidate_high:,.0f} overlaps target ${band_low:,.0f}-${band_high:,.0f} band")
        else:
            score -= 30.0
            dismiss = True
            notes.append(f"revenue range ${candidate_low:,.0f}-${candidate_high:,.0f} outside target ${band_low:,.0f}-${band_high:,.0f} band, dismissed")

    if collateral:
        score += 10.0
        notes.append("collateral description present -- real recent financing activity")

    logistics_flavored = _is_logistics_flavored(business_name, collateral)
    if logistics_flavored:
        score += 15.0
        notes.append("logistics/trucking keyword match -- eligible for free FMCSA fleet-size check")

    # This source has no real Industry column at all (just business_name
    # + an optional collateral_description) -- the free keyword
    # classifier is this silo's ONLY industry signal, not a fallback
    # onto a real one the way mca_history_batch.py uses it. See
    # app/services/industry_classifier.py.
    tag = guess_industry_tag(business_name, collateral)
    if tag:
        score += 5.0
        notes.append(f"industry pool inferred from business name/collateral: {tag}")

    return max(0.0, min(100.0, score)), dismiss, notes


def tier_for_score(score: float) -> str:
    settings = get_settings()
    if score >= settings.ucc_batch_hot_score_threshold:
        return "hot"
    if score >= settings.ucc_batch_medium_score_threshold:
        return "medium"
    return "cold"


def _duplicate_of_existing_lead(db: Session, business_name: str, api_key: str) -> bool:
    """Interzoid fuzzy match (falls back to substring per
    entity_matching.names_match) against every existing MasterLogEntry
    business_name -- avoids re-pitching a merchant already in the book.
    O(n) per candidate against the full Master Log; fine at hundreds of
    rows, worth revisiting (a materialized name index) if Master Log V2
    itself grows into the thousands."""
    existing_names = db.execute(select(MasterLogEntry.business_name)).scalars().all()
    return any(entity_matching.names_match(api_key, business_name, existing) for existing in existing_names)


def import_ucc_batch_csv(db: Session, file_content: bytes) -> dict:
    """Parses the uploaded UCC batch CSV into SiloCandidate rows
    (silo=UCC_BATCH_SILO, status=PENDING) -- staged, not counted toward
    brain.py's threshold until a caller promotes one off the dialer. Does
    not auto-run FMCSA enrichment; see enrich_pending_candidates for
    that, same "import, then separately enrich" split as
    enrichment_orchestrator.py's existing lead-CSV path."""
    settings = get_settings()
    text = file_content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None or not REQUIRED_CSV_COLUMNS.issubset(set(reader.fieldnames)):
        missing = REQUIRED_CSV_COLUMNS - set(reader.fieldnames or [])
        return {"error": f"CSV missing required column(s): {', '.join(sorted(missing))}", "imported": 0}

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

        notes_lines = [f"UCC batch import, row {i + 1}.", f"Tier: {tier.upper()} (score {score:.0f})."]
        notes_lines.extend(score_notes)
        if row.get("secured_party"):
            notes_lines.append(f"Secured party: {row['secured_party']}")
        if row.get("filing_date"):
            notes_lines.append(f"Filing date: {row['filing_date']}")

        db.add(
            SiloCandidate(
                silo=UCC_BATCH_SILO,
                company_name=business_name,
                contact_name=row.get("contact_name") or None,
                phone=row.get("phone") or None,
                email=row.get("email") or None,
                source_reference=f"ucc_batch_row:{i + 1} [ucc_batch_upload]",
                notes="\n".join(notes_lines),
                score=score,
                status=status,
                origin_source=TelemetrySource.UCC_BATCH_UPLOAD,
                industry_tag=guess_industry_tag(business_name, row.get("collateral_description")),
            )
        )
        imported += 1

    db.commit()
    return {
        "imported": imported,
        "duplicates_skipped": duplicates,
        "dismissed_on_revenue_filter": dismissed,
        "tier_counts": tier_counts,
    }


def enrich_pending_candidates(db: Session, limit: int = 50) -> dict:
    """Runs the free FMCSA fleet-size check against logistics-flavored
    PENDING UCC Batch Silo candidates that haven't been checked yet
    (no 'FMCSA:' line in notes). Paced by `limit` per call, same
    "don't burst-hammer an API" posture as enrichment_sweep_batch_size --
    call this repeatedly (a scheduled sweep or a manual batch button)
    rather than attempting the whole silo in one request."""
    settings = get_settings()
    adapter = FMCSAAdapter(api_key=settings.fmcsa_webkey)
    if not adapter.enabled:
        return {"checked": 0, "detail": "FMCSA not configured (fmcsa_webkey unset) -- adapter inert"}

    candidates = db.execute(
        select(SiloCandidate).where(
            SiloCandidate.silo == UCC_BATCH_SILO,
            SiloCandidate.status == SiloCandidateStatus.PENDING,
        )
    ).scalars().all()

    checked = 0
    fleet_confirmed = 0
    for candidate in candidates:
        if checked >= limit:
            break
        if candidate.notes and "FMCSA:" in candidate.notes:
            continue
        if not _is_logistics_flavored(candidate.company_name, None):
            continue

        query = EnrichmentQuery(entity_uid=str(candidate.candidate_uid), business_name=candidate.company_name)
        payload = adapter.enrich(query) if adapter.enabled else {}
        checked += 1

        if payload.get("matched"):
            power_units = payload.get("total_power_units")
            line = f"FMCSA: matched {payload.get('legal_name') or candidate.company_name}, {power_units if power_units is not None else 'unknown'} power units, safety rating {payload.get('safety_rating') or 'unrated'}."
            if power_units is not None and power_units >= 25:
                candidate.score = min(100.0, (candidate.score or 0.0) + 15.0)
                fleet_confirmed += 1
                line += " 25+ truck threshold cleared (+15 score)."
        else:
            line = "FMCSA: no carrier match found."

        candidate.notes = f"{candidate.notes or ''}\n{line}".strip()

    db.commit()
    return {"checked": checked, "fleet_confirmed": fleet_confirmed}
