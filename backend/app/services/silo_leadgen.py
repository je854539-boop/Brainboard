"""Automated silo lead-gen sweep: runs every configured macro telemetry
adapter, then derives SiloCandidate rows from whatever it just ingested.
This is "the brain doing lead gen through the silos" -- a mechanical
sweep-and-derive pass, not a black box; every candidate it creates traces
back to the exact TelemetryEvent that produced it via source_reference.

Each telemetry source is mapped to the silo(s) its data is actually
relevant to. Sources with no configured API key simply produce zero
TelemetryEvents (see telemetry/base.py), so this is a safe no-op until
you wire up credentials.
"""

from sqlalchemy.orm import Session

from app.models.enums import SiloName, TelemetrySource
from app.models.orm import SiloCandidate, TelemetryEvent
from app.services import telemetry

SOURCE_TO_SILOS: dict[TelemetrySource, list[SiloName]] = {
    # CME Macro Funnel is deliberately fed by a wide roster of sources --
    # "Chicago commodities market swings and crashes" per spec. CME Globex
    # is the direct futures-price feed; everything else below it is a
    # leading indicator (conflict/supply-chain-shock news, vessel/AIS
    # traffic on grain-export corridors, lock closures on the barge
    # system, import/export flow, land data, and company-status signals)
    # that tends to move ahead of the price itself showing up on Globex.
    TelemetrySource.CME_GLOBEX: [SiloName.CME_MACRO_FUNNEL, SiloName.TARIFF_SILO],
    TelemetrySource.GDELT: [SiloName.CME_MACRO_FUNNEL],
    TelemetrySource.GFW_4WINGS: [SiloName.CME_MACRO_FUNNEL],
    TelemetrySource.SEAVANTAGE: [SiloName.OIL_GAS_REFINING_SILO, SiloName.CME_MACRO_FUNNEL],
    TelemetrySource.IMPORT_GENIUS: [
        SiloName.HEAVY_MACHINERY_INDUSTRIAL_EQUIPMENT_SILO,
        SiloName.FOOD_PROCESSING_FABRICATION_SILO,
        SiloName.TARIFF_SILO,
        SiloName.CME_MACRO_FUNNEL,
    ],
    TelemetrySource.DATALASTIC: [SiloName.OIL_GAS_REFINING_SILO, SiloName.CME_MACRO_FUNNEL],
    TelemetrySource.VESSELFINDER: [SiloName.OIL_GAS_REFINING_SILO, SiloName.CME_MACRO_FUNNEL],
    TelemetrySource.REGRID: [SiloName.AGRICULTURE_GRAIN_HANDLING_SILO, SiloName.CME_MACRO_FUNNEL],
    TelemetrySource.COBALT_INTELLIGENCE: [SiloName.CME_MACRO_FUNNEL],
    TelemetrySource.APOLLO: [SiloName.CME_MACRO_FUNNEL],
    TelemetrySource.USACE: [SiloName.CME_MACRO_FUNNEL],
    TelemetrySource.UCC_FILINGS: [
        SiloName.FOOD_PROCESSING_FABRICATION_SILO,
        SiloName.HEAVY_MACHINERY_INDUSTRIAL_EQUIPMENT_SILO,
    ],
    TelemetrySource.SOS_REGISTRIES: [SiloName.HEALTHCARE_PHARMA_SILO, SiloName.ECOMMERCE_FULFILLMENT_SILO],
    TelemetrySource.HIGHERGOV: [SiloName.HIGHERGOV_FUNNEL],
    # A recall often means a business needs financing to cover remediation,
    # inventory write-off, or legal costs -- a genuine lead-gen signal.
    TelemetrySource.OPENFDA: [SiloName.HEALTHCARE_PHARMA_SILO],
}


def _company_name_from_event(event: TelemetryEvent) -> str:
    for key in ("company_name", "consignee_name", "recalling_firm", "entity_name", "debtor_name", "symbol"):
        value = event.payload.get(key)
        if value:
            return str(value)
    return event.title


def run_silo_leadgen(db: Session) -> dict:
    """Ingests fresh telemetry from every configured adapter, then derives
    one SiloCandidate per (event, mapped silo) pair. Returns a summary of
    events ingested per source and candidates created per silo."""
    ingested_by_source: dict[str, dict] = {}
    for adapter in telemetry.all_adapters():
        ingested_by_source[adapter.source.value] = adapter.ingest(db)

    candidates_by_silo: dict[str, int] = {silo.value: 0 for silo in SiloName}

    fresh_events = (
        db.query(TelemetryEvent)
        .filter(TelemetryEvent.source.in_(SOURCE_TO_SILOS.keys()))
        .order_by(TelemetryEvent.ingested_at.desc())
        .limit(500)
        .all()
    )

    for event in fresh_events:
        for silo in SOURCE_TO_SILOS.get(event.source, []):
            source_reference = f"telemetry_event:{event.id} [{event.source.value}] {event.title}"
            already_derived = (
                db.query(SiloCandidate)
                .filter(SiloCandidate.silo == silo, SiloCandidate.source_reference == source_reference)
                .first()
            )
            if already_derived:
                continue

            db.add(
                SiloCandidate(
                    silo=silo,
                    company_name=_company_name_from_event(event),
                    source_reference=source_reference,
                )
            )
            candidates_by_silo[silo.value] += 1

    db.commit()
    return {"telemetry_ingested": ingested_by_source, "candidates_created": candidates_by_silo}
