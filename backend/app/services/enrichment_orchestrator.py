"""Runs every configured enrichment adapter against one lead and logs the
results -- the engine behind the Lead Enrichment dashboard section."""

import asyncio
import csv
import io
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import ActivityEventType, ActivitySource, CoBroker
from app.models.orm import EnrichmentResult, MasterLogEntry
from app.services import pipeline
from app.services.enrichment import EnrichmentQuery, all_enrichment_adapters

logger = logging.getLogger("brainboard.enrichment")


def enrich_lead(db: Session, lead: MasterLogEntry) -> list[EnrichmentResult]:
    query = EnrichmentQuery(
        entity_uid=str(lead.lead_uid),
        business_name=lead.business_name,
        contact_name=lead.contact_name,
        phone=lead.phone,
        email=lead.email,
        state=lead.state,
    )

    results = []
    for adapter in all_enrichment_adapters():
        result = adapter.run(db, query)
        results.append(result)

    succeeded = [r.source.value for r in results if r.error is None]
    failed = [r.source.value for r in results if r.error is not None]
    summary = f"succeeded: {','.join(succeeded) or '(none)'}; failed: {','.join(failed) or '(none)'}"
    pipeline.log_activity(
        db, lead.lead_uid, ActivityEventType.ENRICHMENT, ActivitySource.SYSTEM,
        field_name="enrichment_run", new_value=summary,
    )
    db.commit()
    return results


REQUIRED_CSV_COLUMNS = {"business_name", "co_broker"}
OPTIONAL_CSV_COLUMNS = {
    "contact_name", "phone", "email", "state", "annual_revenue",
    "dossier_drive_link", "financials_link", "transcripts_link",
}


def import_leads_csv(db: Session, file_content: bytes) -> dict:
    """Parses an uploaded CSV of leads, creates a MasterLogEntry per valid
    row (source=UI), and returns a summary. Does not auto-run enrichment
    -- that's a separate explicit action per lead/batch."""
    text = file_content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None or not REQUIRED_CSV_COLUMNS.issubset(set(reader.fieldnames)):
        return {"created": 0, "skipped": 0, "errors": [f"CSV must include columns: {', '.join(sorted(REQUIRED_CSV_COLUMNS))}"]}

    created, skipped, errors = 0, 0, []
    for row_number, row in enumerate(reader, start=2):
        business_name = (row.get("business_name") or "").strip()
        co_broker_raw = (row.get("co_broker") or "").strip()
        if not business_name or not co_broker_raw:
            skipped += 1
            errors.append(f"row {row_number}: missing business_name or co_broker")
            continue
        try:
            co_broker = CoBroker(co_broker_raw)
        except ValueError:
            skipped += 1
            errors.append(f"row {row_number}: invalid co_broker {co_broker_raw!r}")
            continue

        extra_fields = {}
        for field in OPTIONAL_CSV_COLUMNS:
            value = (row.get(field) or "").strip()
            if not value:
                continue
            if field == "annual_revenue":
                try:
                    value = float(value.replace(",", "").replace("$", ""))
                except ValueError:
                    continue
            extra_fields[field] = value

        pipeline.create_lead(db, business_name=business_name, co_broker=co_broker, source=ActivitySource.UI, **extra_fields)
        created += 1

    return {"created": created, "skipped": skipped, "errors": errors[:50]}


def sweep_unenriched_leads(db: Session, batch_size: int | None = None) -> int:
    """Finds leads that have never been enriched (zero EnrichmentResult
    rows) and runs the full adapter set against them, oldest first --
    this is what actually connects every new lead to every configured API
    without enrichment blocking lead creation itself. Capped per call via
    batch_size so one cycle can't burst-hammer paid APIs against a large
    backlog (e.g. an initial real-Sheet sync); the rest get picked up on
    the next cycle."""
    settings = get_settings()
    cap = batch_size if batch_size is not None else settings.enrichment_sweep_batch_size

    already_enriched = select(EnrichmentResult.entity_uid).distinct()
    leads = db.execute(
        select(MasterLogEntry)
        .where(MasterLogEntry.lead_uid.not_in(already_enriched))
        .order_by(MasterLogEntry.created_at.asc())
        .limit(cap)
    ).scalars().all()

    for lead in leads:
        enrich_lead(db, lead)
    return len(leads)


# ---------------------------------------------------------------------------
# Background sweep -- must not block the FastAPI event loop
# ---------------------------------------------------------------------------

_sweep_task: asyncio.Task | None = None


async def _sweep_loop() -> None:
    from app.database import SessionLocal

    while True:
        try:
            db = SessionLocal()
            try:
                swept = await asyncio.to_thread(sweep_unenriched_leads, db)
                if swept:
                    logger.info("Enrichment sweep: ran %d lead(s) through every configured adapter", swept)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 -- one bad cycle must not kill the loop
            logger.exception("Enrichment sweep failed -- will retry next cycle")
        await asyncio.sleep(get_settings().enrichment_sweep_interval_seconds)


def start_background_sweep() -> asyncio.Task | None:
    """On by default (enrichment_continuous_sweep_enabled) -- every
    adapter here is already individually inert without its own API key
    (see config.py), so there's no extra cost to leaving the sweep itself
    always on; it just finds nothing to charge against until keys are
    added."""
    global _sweep_task
    if not get_settings().enrichment_continuous_sweep_enabled:
        logger.info("Enrichment continuous sweep disabled (set ENRICHMENT_CONTINUOUS_SWEEP_ENABLED=false to restore manual-only)")
        return None
    if _sweep_task is None or _sweep_task.done():
        _sweep_task = asyncio.create_task(_sweep_loop())
        logger.info("Enrichment background sweep started (interval=%ss, batch_size=%s)",
                    get_settings().enrichment_sweep_interval_seconds, get_settings().enrichment_sweep_batch_size)
    return _sweep_task


def stop_background_sweep() -> None:
    global _sweep_task
    if _sweep_task is not None:
        _sweep_task.cancel()
        _sweep_task = None
        logger.info("Enrichment background sweep stopped")
