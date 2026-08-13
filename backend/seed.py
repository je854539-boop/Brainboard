"""Populate the local dev database with demo data so the dashboard is
viewable end-to-end (pipeline, silo grid, enrichment, brain, globe, hazard
curves, surveillance feed) without live Google/telemetry credentials.

Usage: python seed.py   (run inside the backend venv, with DATABASE_URL set)
"""

import datetime as dt
import random
import uuid

from app.database import SessionLocal
from app.models.enums import (
    TERMINAL_ATTRITION_STATUSES,
    ActivityEventType,
    ActivitySource,
    CoBroker,
    MasterLogStatus,
    SiloCandidateStatus,
    SiloName,
    TelemetrySource,
)
from app.models.orm import (
    EnrichmentResult,
    GlobeSignal,
    LeadActivityEvent,
    MasterLogEntry,
    SiloCandidate,
    StatusHistory,
    TelemetryEvent,
)
from app.services import brain, hazard_engine

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
    "Overwatch Cargo Systems", "Palisade Grain Terminal", "Ledger Rock Fulfillment",
    "Switchback Machine Works", "Amberline Poultry Processing", "Driftwood Marine Fueling",
    "Kestrel Point Aerospace", "Thornfield Dairy Logistics", "Copperline Fabrication",
    "Bluestem Ag Equipment", "Rockford Cold Storage", "Wavecrest Container Lines",
    "Sentinel Ridge Manufacturing", "Yellowpine Timber Haulers", "Foxhollow Pharma Supply",
    "Cascadia Freight Brokers", "Ninebark Industrial Coatings", "Longview Grain Cooperative",
    "Ashford Fleet Maintenance", "Brightwater Fulfillment Hub", "Cinderpath CNC Solutions",
    "Deepwell Oilfield Services", "Elmgate Poultry Farms", "Fallowfield Ag Logistics",
    "Graystone Aerospace Parts", "Hearthside Cold Chain", "Ironvale Heavy Haul",
]

FUNNEL_STAGES = [
    MasterLogStatus.NEW_LEAD,
    MasterLogStatus.APP_SENT,
    MasterLogStatus.DOCS_OWED,
    MasterLogStatus.CHASE_DOCS,
    MasterLogStatus.DOCS_IN,
    MasterLogStatus.IN_NEGOTIATION,
]

# Weighted so there's a healthy number of FUNDED outcomes -- both the
# Brain (needs >=10 terminal leads) and the Cox time-varying model (needs
# >=5 funded outcomes) train on this.
TERMINAL_OUTCOMES = [MasterLogStatus.FUNDED] * 5 + list(TERMINAL_ATTRITION_STATUSES) * 2

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

STATES = ["TX", "CA", "FL", "GA", "OH", "AZ", "NC", "IL", "PA", "WA"]
LENDERS = ["OnDeck", "Kabbage", "Rapid Finance", "Credibly", "Fundbox", None]
PAYMENT_FREQS = ["Daily", "Weekly", "Bi-Weekly"]

# lat/lon pairs for demo globe pings, real port/logistics hub locations
GFW_DEMO_POINTS = [
    (29.75, -95.35, "Port of Houston"), (33.77, -118.19, "Port of Long Beach"),
    (1.29, 103.85, "Singapore Strait"), (29.95, -90.07, "Port of New Orleans"),
    (51.95, 4.14, "Port of Rotterdam"), (22.31, 114.17, "Hong Kong waters"),
]
GDELT_DEMO_POINTS = [
    (40.71, -74.01, "New York"), (51.51, -0.13, "London"),
    (41.88, -87.63, "Chicago"), (34.05, -118.24, "Los Angeles"),
]


def seed_master_log(db) -> list[MasterLogEntry]:
    entries = []
    for name in BUSINESS_NAME_POOL:
        co_broker = random.choice(list(CoBroker))
        entered_at = NOW - dt.timedelta(days=random.randint(3, 150))

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
            state=random.choice(STATES),
            annual_revenue=round(random.uniform(300_000, 5_000_000), 2),
            lender=random.choice(LENDERS) if is_terminal else None,
            payment_amt=round(random.uniform(150, 1200), 2) if is_terminal else None,
            payment_freq=random.choice(PAYMENT_FREQS) if is_terminal else None,
            current_balance=round(random.uniform(5_000, 400_000), 2) if is_terminal else None,
            open_positions=random.randint(0, 4),
            credit_score=random.randint(560, 780),
            notes=random.choice([
                "Awaiting updated bank statements.", "Strong cash flow, fast-track candidate.",
                "Competitor also quoting -- pricing sensitive.", "Owner traveling, follow up next week.", None,
            ]),
            dossier_drive_link=f"https://drive.google.com/drive/folders/demo-{uuid.uuid4().hex[:10]}",
            created_at=entered_at,
        )
        db.add(entry)
        db.flush()

        db.add(LeadActivityEvent(
            lead_uid=entry.lead_uid, event_type=ActivityEventType.CREATED, source=ActivitySource.SYSTEM,
            new_value=name, occurred_at=entered_at,
        ))

        ts = entered_at
        prev_status = None
        for status in path:
            ts = ts + dt.timedelta(days=random.uniform(1, 14))
            if ts > NOW:
                ts = NOW
            db.add(StatusHistory(lead_uid=entry.lead_uid, from_status=prev_status, to_status=status, changed_at=ts))
            db.add(LeadActivityEvent(
                lead_uid=entry.lead_uid, event_type=ActivityEventType.STATUS_CHANGE, source=ActivitySource.SYSTEM,
                field_name="status", old_value=prev_status.value if prev_status else None, new_value=status.value,
                occurred_at=ts,
            ))
            # scatter a few note/click/calendar events between stage transitions
            for _ in range(random.randint(0, 3)):
                event_ts = ts - dt.timedelta(days=random.uniform(0, 3))
                if event_ts < entered_at:
                    continue
                event_type = random.choice([
                    ActivityEventType.NOTE_CHANGE, ActivityEventType.CLICK, ActivityEventType.CALENDAR_SYNC,
                ])
                db.add(LeadActivityEvent(
                    lead_uid=entry.lead_uid, event_type=event_type, source=ActivitySource.UI,
                    field_name="dossier_link_opened" if event_type == ActivityEventType.CLICK else None,
                    occurred_at=event_ts,
                ))
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
        TelemetrySource.CME_GLOBEX: ("Grain complex open interest +4.2% WoW [DEMO SEED DATA]", {"demo": True}),
        TelemetrySource.IMPORT_GENIUS: ("New consignee manifest: CNC machining imports [DEMO SEED DATA]", {"demo": True}),
        TelemetrySource.SEAVANTAGE: ("Tanker ETA Port of Houston, 36h [DEMO SEED DATA]", {"demo": True}),
        TelemetrySource.UCC_FILINGS: ("UCC-1 filed: equipment lien, TX SOS [DEMO SEED DATA]", {"demo": True}),
        TelemetrySource.SOS_REGISTRIES: ("New entity formation: cold-chain logistics LLC [DEMO SEED DATA]", {"demo": True}),
        TelemetrySource.REGRID: ("Parcel permit filed: grain elevator expansion [DEMO SEED DATA]", {"demo": True}),
        TelemetrySource.DRIVE_OCR: ("Bank statement OCR: ending balance extracted [DEMO SEED DATA]", {"demo": True}),
        TelemetrySource.HIGHERGOV: ("Contract award notice: fleet maintenance IDIQ [DEMO SEED DATA]", {"demo": True}),
        TelemetrySource.OPENFDA: (
            "FDA enforcement: Foxhollow Pharma Supply [DEMO SEED DATA]",
            {"demo": True, "recalling_firm": "Foxhollow Pharma Supply", "classification": "Class II"},
        ),
    }
    for source, (title, payload) in samples.items():
        for _ in range(random.randint(3, 6)):
            db.add(
                TelemetryEvent(
                    source=source,
                    title=title,
                    payload=payload,
                    ingested_at=NOW - dt.timedelta(hours=random.uniform(0, 96)),
                )
            )
    db.commit()


def seed_enrichment(db, leads: list[MasterLogEntry]) -> None:
    sample_leads = random.sample(leads, k=min(5, len(leads)))
    for lead in sample_leads:
        db.add(EnrichmentResult(
            entity_uid=lead.lead_uid, source=TelemetrySource.COBALT_INTELLIGENCE,
            payload={"entity_status": "active", "demo": True}, confidence=0.9,
            created_at=NOW - dt.timedelta(hours=random.uniform(0, 48)),
        ))
        db.add(EnrichmentResult(
            entity_uid=lead.lead_uid, source=TelemetrySource.APOLLO,
            error="apollo is not configured (no API key) [DEMO SEED DATA]",
            created_at=NOW - dt.timedelta(hours=random.uniform(0, 48)),
        ))
    db.commit()


def seed_globe(db) -> None:
    for lat, lon, label in GFW_DEMO_POINTS:
        db.add(GlobeSignal(
            source=TelemetrySource.GFW_4WINGS, latitude=lat, longitude=lon,
            intensity=round(random.uniform(2, 20), 1), title=f"Marine traffic, {label} [DEMO SEED DATA]",
            payload={"demo": True}, observed_at=NOW - dt.timedelta(hours=random.uniform(0, 72)),
        ))
    for lat, lon, label in GDELT_DEMO_POINTS:
        db.add(GlobeSignal(
            source=TelemetrySource.GDELT, latitude=lat, longitude=lon,
            title=f"GDELT conflict-zone monitoring point, {label} [DEMO SEED DATA -- not a real event]",
            payload={"demo": True}, observed_at=NOW - dt.timedelta(hours=random.uniform(0, 72)),
        ))
    db.commit()


def main():
    db = SessionLocal()
    try:
        db.query(EnrichmentResult).delete()
        db.query(GlobeSignal).delete()
        db.query(LeadActivityEvent).delete()
        db.query(StatusHistory).delete()
        db.query(TelemetryEvent).delete()
        db.query(SiloCandidate).delete()
        db.query(MasterLogEntry).delete()
        db.commit()

        leads = seed_master_log(db)
        seed_silos(db)
        seed_telemetry(db)
        seed_enrichment(db, leads)
        seed_globe(db)

        refreshed = hazard_engine.refresh_hazard_snapshots(db)
        scored = brain.refresh_shadow_scores(db)
        cox = hazard_engine.fit_cox_time_varying(db)

        print(f"Seeded {len(BUSINESS_NAME_POOL)} leads, silo candidates across {len(list(SiloName))} silos, "
              f"demo telemetry/enrichment/globe signals.")
        print(f"Refreshed {refreshed} hazard snapshots, {scored} brain shadow scores.")
        print(f"Cox time-varying model: {'fitted' if cox['fitted'] else 'not fitted -- ' + cox['reason']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
