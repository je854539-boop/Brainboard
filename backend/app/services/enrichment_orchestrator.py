"""Runs every configured enrichment adapter against one lead and logs the
results -- the engine behind the Lead Enrichment dashboard section."""

import csv
import io

from sqlalchemy.orm import Session

from app.models.enums import ActivityEventType, ActivitySource, CoBroker
from app.models.orm import EnrichmentResult, MasterLogEntry
from app.services import pipeline
from app.services.enrichment import EnrichmentQuery, all_enrichment_adapters


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
