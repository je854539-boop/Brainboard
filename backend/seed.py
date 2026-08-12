"""Populate the local dev database with demo data so the dashboard is
viewable end-to-end (pipeline, silo grid, hazard curves, surveillance
feed) without live Google/telemetry credentials.

Usage: python seed.py   (run inside the backend venv, with DATABASE_URL set)
"""

import datetime as dt
import random
import uuid

from app.database import SessionLocal
from app.models.enums import (
    TERMINAL_ATTRITION_STATUSES,
    CoBroker,
    MasterLogStatus,
    SiloCandidateStatus,
    SiloName,
    TelemetrySource,
)
from app.models.orm import MasterLogEntry, SiloCandidate, StatusHistory, TelemetryEvent
from app.services import hazard_engine

random.seed(42)
NOW = dt.datetime.now(dt.timezone.utc)

BUSINESS_NAME_POOL = [
    "Apex Cold Chain Fleet", "Northgate Reefer Logistics", "Ironhide CNC Fabrication",
    "Meridian Aerospace Machining", "Sable Ridge Grain Handling", "Vantage Point Ag Co-op",
    "Blackwell Heavy Equipment", "Corvus Precision Manufacturing", "Tallow & Timber Freight",
    "Redline Specialized Carriers", "Granite State Fulfillment", "Coastal Reef Oil Services",
    "Highplains Cattle Logistics", "Vireo Controlled Environment Ag", "Ferrous Point Industrial",
    "Sundowner Fleet Solutions", "Anchor Bay E-Commerce Fulfillment", "Cascade Pharma Distribution",
    "Ninth Meridian Heavy Haul", "Steelhead Manufacturing Group", "Prairie Fire Grain Co",
    "Truelock Precision Aerospace", "Hollow Ridge Refining", "Basecamp Fleet Freight",
]

FUNNEL_STAGES = [
    MasterLogStatus.NEW_LEAD,
    MasterLogStatus.APP_SENT,
    MasterLogStatus.DOCS_OWED,
    MasterLogStatus.CHASE_DOCS,
    MasterLogStatus.DOCS_IN,
    MasterLogStatus.IN_NEGOTIATION,
]

TERMINAL_OUTCOMES = [MasterLogStatus.FUNDED] * 3 + list(TERMINAL_ATTRITION_STATUSES)

SILO_SOURCE_REFERENCE = {
    SiloName.CME_MACRO_FUNNEL: "CME Globex open-interest spike, grain complex",
    SiloName.TARIFF_SILO: "USTR tariff schedule change, HTS 8701.20",
    SiloName.FOOD_PROCESSING_FABRICATION_SILO: "UCC-1 filed against processing line equipment",
    SiloName.OIL_GAS_REFINING_SILO: "SeaVantage AIS: tanker inbound, Port of Corpus Christi",
    SiloName.HEAVY_MACHINERY_INDUSTRIAL_EQUIPMENT_SILO: "Import Genius manifest: CNC machining centers",
    SiloName.AGRICULTURE_GRAIN_HANDLING_SILO: "Regrid parcel: new grain elevator permit filed",
    SiloName.HEALTHCARE_PHARMA_SILO: "SOS registry: new pharma distribution entity formed",
    SiloName.ECOMMERCE_FULFILLMENT_SILO: "Regrid parcel: fulfillment center groundbreaking",
    SiloName.HIGHERGOV_FUNNEL: "HigherGov contract award notice",
}


def seed_master_log(db) -> list[MasterLogEntry]:
    entries = []
    for name in BUSINESS_NAME_POOL:
        co_broker = random.choice(list(CoBroker))
        entered_at = NOW - dt.timedelta(days=random.randint(3, 120))

        is_terminal = random.random() < 0.55
        path = FUNNEL_STAGES[: random.randint(2, len(FUNNEL_STAGES))]
        final_status = path[-1]
        if is_terminal:
            final_status = random.choice(TERMINAL_OUTCOMES)
            path = path + [final_status]

        entry = MasterLogEntry(
            business_name=name,
            contact_name=random.choice(["J. Alvarez", "M. Chen", "R. Okafor", "T. Whitfield", None]),
            phone=f"({random.randint(200,989)}) {random.randint(200,989)}-{random.randint(1000,9999)}",
            co_broker=co_broker,
            status=final_status,
            loan_amount_requested=round(random.uniform(75_000, 2_500_000), 2),
            notes=random.choice([
                "Awaiting updated bank statements.", "Strong cash flow, fast-track candidate.",
                "Competitor also quoting -- pricing sensitive.", "Owner traveling, follow up next week.", None,
            ]),
            dossier_drive_link=f"https://drive.google.com/drive/folders/demo-{uuid.uuid4().hex[:10]}",
            created_at=entered_at,
        )
        db.add(entry)
        db.flush()

        ts = entered_at
        prev_status = None
        for status in path:
            ts = ts + dt.timedelta(days=random.uniform(1, 14))
            if ts > NOW:
                ts = NOW
            db.add(StatusHistory(lead_uid=entry.lead_uid, from_status=prev_status, to_status=status, changed_at=ts))
            prev_status = status

        if not is_terminal and random.random() < 0.7:
            entry.follow_up_date = NOW + dt.timedelta(days=random.randint(1, 14))

        entries.append(entry)

    db.commit()
    return entries


def seed_silos(db) -> None:
    for silo in SiloName:
        for i in range(random.randint(6, 11)):
            status = random.choices(
                [SiloCandidateStatus.PENDING, SiloCandidateStatus.CONVERTED, SiloCandidateStatus.DISMISSED],
                weights=[0.65, 0.15, 0.20],
            )[0]
            db.add(
                SiloCandidate(
                    silo=silo,
                    company_name=f"{random.choice(BUSINESS_NAME_POOL)} #{i}",
                    contact_name=random.choice(["Ops Manager", "Owner/Operator", "CFO", None]),
                    phone=f"({random.randint(200,989)}) {random.randint(200,989)}-{random.randint(1000,9999)}",
                    source_reference=SILO_SOURCE_REFERENCE[silo],
                    dossier_drive_link=f"https://drive.google.com/drive/folders/demo-{uuid.uuid4().hex[:10]}",
                    score=round(random.uniform(40, 98), 2),
                    status=status,
                    created_at=NOW - dt.timedelta(days=random.randint(0, 45)),
                )
            )
    db.commit()


def seed_telemetry(db) -> None:
    samples = {
        TelemetrySource.CME_GLOBEX: "Grain complex open interest +4.2% WoW [DEMO SEED DATA]",
        TelemetrySource.IMPORT_GENIUS: "New consignee manifest: CNC machining imports [DEMO SEED DATA]",
        TelemetrySource.SEAVANTAGE: "Tanker ETA Port of Houston, 36h [DEMO SEED DATA]",
        TelemetrySource.UCC_FILINGS: "UCC-1 filed: equipment lien, TX SOS [DEMO SEED DATA]",
        TelemetrySource.SOS_REGISTRIES: "New entity formation: cold-chain logistics LLC [DEMO SEED DATA]",
        TelemetrySource.REGRID: "Parcel permit filed: grain elevator expansion [DEMO SEED DATA]",
        TelemetrySource.DRIVE_OCR: "Bank statement OCR: ending balance extracted [DEMO SEED DATA]",
    }
    for source, title in samples.items():
        for _ in range(random.randint(3, 6)):
            db.add(
                TelemetryEvent(
                    source=source,
                    title=title,
                    payload={"demo": True},
                    ingested_at=NOW - dt.timedelta(hours=random.uniform(0, 96)),
                )
            )
    db.commit()


def main():
    db = SessionLocal()
    try:
        db.query(StatusHistory).delete()
        db.query(TelemetryEvent).delete()
        db.query(SiloCandidate).delete()
        db.query(MasterLogEntry).delete()
        db.commit()

        seed_master_log(db)
        seed_silos(db)
        seed_telemetry(db)

        refreshed = hazard_engine.refresh_hazard_snapshots(db)
        print(f"Seeded {len(BUSINESS_NAME_POOL)} leads, silo candidates across {len(list(SiloName))} silos, "
              f"demo telemetry, and refreshed {refreshed} hazard snapshots.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
