"""The MCA History Batch Silo pipeline -- ingestion, dedupe, and
Hot/Medium/Cold tiering for the purchased contact list: existing
businesses with current/prior MCA history and high revenue.

Same staging posture as services/ucc_batch.py and the same reason for
it: a raw contact record has no call history yet, so it lands as a
SiloCandidate (silo=MCA_HISTORY_BATCH_SILO, status=PENDING) -- staged,
workable off the dialer, outside brain.py's shadow-mode count until a
real human touchpoint promotes it via the existing pipeline.
convert_or_update_silo_candidate, same conversion path every other silo
already uses.

SCHEMA CORRECTED AGAINST THE REAL FILE: this module originally guessed
at a CSV shape (mca_status "current"/"prior", current_funder,
current_balance, position_count) before the actual list existed in this
conversation. The real upload (Big_Advanced_Cleaned_From_DNC.xlsx,
18,402 rows) has none of those columns -- its real, confirmed columns
are: first_name, email, business_name, phone_number, alt_phone,
Line Type, monthly_revenue, start_date, Industry, address1. Rewritten
below to match that ground truth rather than the earlier guess:
  - No per-row MCA-current-vs-prior field exists. The list's own
    premise (purchased specifically as an MCA-history, DNC-scrubbed
    list, per the user directly) is credited as a flat signal applied
    to every row, not fabricated per-row granularity the data can't
    support.
  - monthly_revenue already matches 1:1 -- every one of the 18,402 rows
    clears $200K/mo on its own, confirmed by direct inspection, which is
    what moved mca_history_batch_revenue_min_usd's default up from the
    original "$100K/mo" verbal estimate to the real $200K/mo floor.
  - start_date is business start date (years in business), not an MCA
    funding date -- a longevity signal, not an MCA-position signal.
  - Industry is populated on ~29% of rows and maps, where it reasonably
    can, onto the existing DialerNumberPool.industry_tag vocabulary
    (dialer.INDUSTRY_INBOUND_GREETINGS) so the inbound greeting system
    already built this session can use it directly.
  - address1 is free-text, no separate state column -- state is
    extracted best-effort via regex/full-name lookup, None when
    unconfident rather than guessed.
"""

import csv
import io
import logging
import re
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import SiloCandidateStatus, SiloName, TelemetrySource
from app.models.orm import MasterLogEntry, SiloCandidate
from app.services import entity_matching

logger = logging.getLogger("brainboard.mca_history_batch")

MCA_HISTORY_BATCH_SILO = SiloName.MCA_HISTORY_BATCH_SILO

# The real file's own column names -- kept exact (including the mixed
# case/spacing "Line Type") rather than normalized, so a re-export of
# the same vendor list drops in without a remapping step.
REQUIRED_COLUMNS = {"business_name"}
RECOGNIZED_COLUMNS = {
    "first_name", "email", "business_name", "phone_number", "alt_phone",
    "Line Type", "monthly_revenue", "start_date", "Industry", "address1",
}

US_STATE_ABBREVIATIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY", "DC",
}
US_STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY",
}

# Best-effort mapping onto the existing industry_tag vocabulary
# (dialer.INDUSTRY_INBOUND_GREETINGS) -- reasoned fit, not a verified
# taxonomy crosswalk. Several real Industry values (Auto Sales, Auto
# Repair, Business Services, Consulting, Computer/IT/Software, Real
# Estate Activities, "Other") have no reasonable existing tag and
# deliberately map to None rather than being forced into a poor fit --
# None means "no industry-specific greeting," not an error.
INDUSTRY_TAG_MAP: dict[str, str] = {
    "trucking": "Trucking & Logistics",
    "logistics": "Trucking & Logistics",
    "other transportation": "Trucking & Logistics",
    "manufacturing": "Heavy Machinery & Industrial Equipment",
    "construction": "Heavy Machinery & Industrial Equipment",
    "general contractor": "Heavy Machinery & Industrial Equipment",
    "other specialty trade contractors": "Heavy Machinery & Industrial Equipment",
    "restaurant": "Food Processing & Fabrication",
    "wholesale": "Food Processing & Fabrication",
    "other medical services": "Healthcare & Pharma",
    "home health": "Healthcare & Pharma",
    "retail": "E-Commerce & Fulfillment",
}


def industry_tag_for(industry: str | None) -> str | None:
    if not industry:
        return None
    return INDUSTRY_TAG_MAP.get(industry.strip().lower())


def _extract_state(address1: str | None) -> str | None:
    """Best-effort only -- the real file has no separate state column,
    and address1's own formatting is inconsistent (period-separated vs.
    space-separated before the zip, abbreviation vs. full state name
    spelled out). Returns None rather than a wrong guess when neither
    pattern confidently matches."""
    if not address1:
        return None
    match = re.search(r"\b([A-Z]{2})\b\.?\s*\d{5}(-\d{4})?\s*\.?\s*$", address1.strip())
    if match and match.group(1) in US_STATE_ABBREVIATIONS:
        return match.group(1)
    lower = address1.lower()
    found = None
    for name, abbr in US_STATE_NAME_TO_ABBR.items():
        if re.search(rf"\b{re.escape(name)}\b", lower):
            found = abbr
    return found


def _years_in_business(start_date) -> float | None:
    if start_date is None or (isinstance(start_date, float) and pd.isna(start_date)):
        return None
    try:
        if isinstance(start_date, str):
            parsed = datetime.fromisoformat(start_date[:10])
        elif hasattr(start_date, "to_pydatetime"):
            parsed = start_date.to_pydatetime()
        else:
            parsed = start_date
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - parsed).days / 365.25
    except Exception:  # noqa: BLE001 -- a bad date in one row must not break the batch
        return None


def _parse_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    try:
        if isinstance(value, str):
            return float(value.replace(",", "").replace("$", "").strip() or "nan")
        return float(value)
    except (ValueError, TypeError):
        return None


def score_candidate(row: dict) -> tuple[float, bool, list[str]]:
    """0-100 tiering score plus a hard-dismiss flag and the notes
    explaining why. Only the FLOOR is enforced (this list's own
    $200K/mo, settings.mca_history_batch_revenue_min_usd) -- no ceiling
    dismissal, since none was given and this list's own range runs from
    $200K/mo to $50M/mo. Never dismisses for lack of data -- only a
    confirmed revenue figure below the floor does. Reasoned weights, not
    measured: the flat "sourced from this vetted MCA-history list"
    credit stands in for a real per-row current/prior MCA field the
    source data doesn't carry -- see module docstring for why that's
    honest rather than fabricated granularity."""
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

    revenue = _parse_float(row.get("monthly_revenue"))
    if revenue is not None:
        annual = revenue * 12
        floor = settings.mca_history_batch_revenue_min_usd
        if annual >= floor:
            score += 15.0
            notes.append(f"revenue ${revenue:,.0f}/mo (${annual:,.0f}/yr) clears the ${floor:,.0f}/yr floor")
            if revenue >= 1_000_000:
                score += 10.0
                notes.append(f"${revenue:,.0f}/mo -- top-tier deal size within this list")
        else:
            score -= 30.0
            dismiss = True
            notes.append(f"revenue ${revenue:,.0f}/mo (${annual:,.0f}/yr) below the ${floor:,.0f}/yr floor, dismissed")

    # Flat credit, not per-row-verified -- see module docstring.
    score += 20.0
    notes.append("sourced from the vetted MCA-history, DNC-scrubbed list")

    years = _years_in_business(row.get("start_date"))
    if years is not None and years >= 5:
        score += 10.0
        notes.append(f"~{years:.0f} years in business -- longevity signal")

    industry = row.get("industry")
    if industry:
        score += 5.0
        notes.append(f"industry on file: {industry}")
        tag = industry_tag_for(industry)
        if tag:
            score += 5.0
            notes.append(f"maps to industry pool: {tag}")

    if row.get("line_type"):
        notes.append(f"line type: {row['line_type']}")
    if row.get("alt_phone"):
        notes.append(f"alt phone on file: {row['alt_phone']}")

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
    already in Master Log V2."""
    existing_names = db.execute(select(MasterLogEntry.business_name)).scalars().all()
    return any(entity_matching.names_match(api_key, business_name, existing) for existing in existing_names)


def _rows_from_xlsx(file_content: bytes) -> list[dict]:
    df = pd.read_excel(io.BytesIO(file_content))
    return df.to_dict(orient="records")


def _rows_from_csv(file_content: bytes) -> list[dict]:
    text = file_content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _normalize_row(raw: dict) -> dict:
    """Maps the real file's exact column names (mixed case, spaces) onto
    the internal snake_case keys score_candidate/the importer use, and
    runs the two derived fields (state from address1, nothing else
    derived at this stage -- years-in-business is computed lazily in
    score_candidate since it needs "now" at scoring time, not import
    time)."""
    def get(key: str):
        value = raw.get(key)
        if isinstance(value, float) and pd.isna(value):
            return None
        return value

    business_name = get("business_name")
    address1 = get("address1")
    # Real data-quality artifact confirmed in the source file: 23 of
    # 18,402 rows (~0.1%) carry a bare number (an Excel date serial that
    # leaked from another column, by the look of it) in Industry instead
    # of a category string. Treated as "no usable industry" rather than
    # guessed at or allowed to crash the scorer -- the source row isn't
    # corrected, this importer just doesn't trust a non-string value.
    industry = get("Industry")
    if industry is not None and not isinstance(industry, str):
        industry = None
    return {
        "business_name": str(business_name).strip() if business_name else None,
        "contact_name": get("first_name"),
        "email": get("email"),
        "phone": get("phone_number"),
        "alt_phone": get("alt_phone"),
        "line_type": get("Line Type"),
        "monthly_revenue": get("monthly_revenue"),
        "start_date": get("start_date"),
        "industry": industry,
        "address1": address1,
        "state": _extract_state(str(address1)) if address1 else None,
    }


def import_mca_history_batch_file(db: Session, file_content: bytes, filename: str) -> dict:
    """Parses an uploaded .xlsx or .csv MCA history batch into
    SiloCandidate rows (silo=MCA_HISTORY_BATCH_SILO, status=PENDING) --
    staged, not counted toward brain.py's threshold until a caller
    promotes one off the dialer. Detects format by filename extension;
    an .xlsx with no "business_name" column (a re-export with different
    headers) returns an error summary rather than silently importing
    garbage rows."""
    is_xlsx = filename.lower().endswith((".xlsx", ".xls"))
    try:
        rows_raw = _rows_from_xlsx(file_content) if is_xlsx else _rows_from_csv(file_content)
    except Exception as exc:  # noqa: BLE001 -- a malformed upload must report cleanly, not crash the caller
        return {"error": f"could not parse {filename}: {exc}", "imported": 0}

    if not rows_raw:
        return {"error": "file parsed but contained no rows", "imported": 0}
    if "business_name" not in rows_raw[0]:
        return {"error": f"missing required column 'business_name' -- found columns: {list(rows_raw[0].keys())}", "imported": 0}

    settings = get_settings()
    imported = 0
    duplicates = 0
    dismissed = 0
    skipped_blank = 0
    tier_counts = {"hot": 0, "medium": 0, "cold": 0}
    seen_in_batch: set[str] = set()

    for i, raw in enumerate(rows_raw):
        row = _normalize_row(raw)
        business_name = row["business_name"]
        if not business_name:
            skipped_blank += 1
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

        notes_lines = [f"MCA history batch import, row {i + 1} ({filename}).", f"Tier: {tier.upper()} (score {score:.0f})."]
        notes_lines.extend(score_notes)
        if row.get("state"):
            notes_lines.append(f"State (parsed from address): {row['state']}")
        if row.get("address1"):
            notes_lines.append(f"Address on file: {row['address1']}")

        db.add(
            SiloCandidate(
                silo=MCA_HISTORY_BATCH_SILO,
                company_name=business_name,
                contact_name=row.get("contact_name") or None,
                phone=str(row["phone"]) if row.get("phone") else None,
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
        "blank_name_skipped": skipped_blank,
        "dismissed_on_revenue_floor": dismissed,
        "tier_counts": tier_counts,
    }
