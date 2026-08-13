"""Automated silo lead-gen sweep: runs every configured macro telemetry
adapter, then derives SiloCandidate rows from what it just ingested.

Eight of the nine silos use a flat mapping (SOURCE_TO_SILOS below): one
telemetry event from a mapped source produces one candidate in that silo,
1:1, no cross-source logic. That is the right model when a single
provider's record IS the lead signal (e.g. a UCC filing, an SOS status
change, an openFDA recall).

**CME Macro Funnel is different by design and is NOT in SOURCE_TO_SILOS.**
"Chicago commodities market swings and crashes" isn't one signal, it's a
sequential dependency chain -- a price move alone isn't a lead, a company
name alone isn't a lead, a company with no lending appetite isn't a lead
worth calling. run_cme_macro_funnel_waterfall() below implements the real
4-stage funnel:

  1. SIGNAL   -- CME Globex (price) + GDELT (news) + GFW 4Wings (ocean) --
                 detect that something macro is actually happening. No
                 active signal = the waterfall stops here. Nothing below
                 this stage runs, and zero candidates are created.
  2. IDENTIFY -- Import Genius (customs manifests) + Regrid (parcel
                 ownership) -- turn "corn futures are volatile" into
                 actual company names, corroborated by SeaVantage/
                 Datalastic/VesselFinder/USACE vessel & barge activity in
                 the same window. This is where SiloCandidate rows first
                 get created -- gated behind stage 1, not independent of it.
  3. FILTER   -- Cobalt Intelligence SOS standing -- a company in
                 suspended/dissolved standing isn't fundable no matter how
                 loud the macro signal is; dismissed here, not surfaced.
  4. CONTACT  -- Apollo.io org search -- attach phone/contact info to
                 candidates that survived the filter, same "surface what's
                 missing" pattern as the Enrichment page.

Every candidate this produces carries in its `notes` field exactly which
signal gated it in, which trade-data source identified it, and what the
lending-appetite check found -- so nothing here is a black box; every row
traces back to the real telemetry that produced it via source_reference,
same guarantee the flat mapping gives the other 8 silos.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.enums import SiloCandidateStatus, SiloName, TelemetrySource
from app.models.orm import SiloCandidate, TelemetryEvent
from app.services import telemetry

SOURCE_TO_SILOS: dict[TelemetrySource, list[SiloName]] = {
    TelemetrySource.CME_GLOBEX: [SiloName.TARIFF_SILO],
    TelemetrySource.SEAVANTAGE: [SiloName.OIL_GAS_REFINING_SILO],
    TelemetrySource.IMPORT_GENIUS: [
        SiloName.HEAVY_MACHINERY_INDUSTRIAL_EQUIPMENT_SILO,
        SiloName.FOOD_PROCESSING_FABRICATION_SILO,
        SiloName.TARIFF_SILO,
    ],
    TelemetrySource.DATALASTIC: [SiloName.OIL_GAS_REFINING_SILO],
    TelemetrySource.VESSELFINDER: [SiloName.OIL_GAS_REFINING_SILO],
    TelemetrySource.REGRID: [SiloName.AGRICULTURE_GRAIN_HANDLING_SILO],
    TelemetrySource.UCC_FILINGS: [
        SiloName.FOOD_PROCESSING_FABRICATION_SILO,
        SiloName.HEAVY_MACHINERY_INDUSTRIAL_EQUIPMENT_SILO,
    ],
    TelemetrySource.SOS_REGISTRIES: [SiloName.HEALTHCARE_PHARMA_SILO, SiloName.ECOMMERCE_FULFILLMENT_SILO],
    TelemetrySource.HIGHERGOV: [SiloName.HIGHERGOV_FUNNEL],
    # A recall often means a business needs financing to cover remediation,
    # inventory write-off, or legal costs -- a genuine lead-gen signal.
    TelemetrySource.OPENFDA: [SiloName.HEALTHCARE_PHARMA_SILO],
    # GDELT, GFW_4WINGS, COBALT_INTELLIGENCE, APOLLO, and USACE feed ONLY
    # CME Macro Funnel, and are deliberately absent from this flat mapping
    # -- they're consumed by run_cme_macro_funnel_waterfall() below instead,
    # which is a sequential funnel, not a 1:1 event-to-candidate dump.
}


def _company_name_from_event(event: TelemetryEvent) -> str:
    for key in ("company_name", "consignee_name", "recalling_firm", "entity_name", "debtor_name", "symbol"):
        value = event.payload.get(key)
        if value:
            return str(value)
    return event.title


def _fresh_events(db: Session, source: TelemetrySource, limit: int = 50) -> list[TelemetryEvent]:
    return (
        db.query(TelemetryEvent)
        .filter(TelemetryEvent.source == source)
        .order_by(TelemetryEvent.ingested_at.desc())
        .limit(limit)
        .all()
    )


# ---------------------------------------------------------------------------
# CME Macro Funnel -- 4-stage sequential waterfall
# ---------------------------------------------------------------------------

CME_MACRO_SILO = SiloName.CME_MACRO_FUNNEL

# Stage 1: a same-day |close-open| move of this size on any tracked CBOT
# grain/oilseed contract counts as an active price-shock signal.
PRICE_SHOCK_PCT = 0.02

_DISTRESS_STATUS_KEYWORDS = ("suspended", "dissolved", "revoked", "inactive", "delinquent", "forfeited")
_GOOD_STANDING_KEYWORDS = ("active", "good standing", "in good standing", "current")


@dataclass
class MacroSignal:
    kind: str
    description: str
    source_reference: str


def _stage1_detect_signals(db: Session) -> list[MacroSignal]:
    """The gate. Returns every active macro signal found across the three
    leading-indicator channels. An empty return means the waterfall stops
    here -- stages 2-4 never run and zero candidates get created."""
    signals: list[MacroSignal] = []

    for event in _fresh_events(db, TelemetrySource.CME_GLOBEX, limit=50):
        open_, close = event.payload.get("open"), event.payload.get("close")
        if not open_ or not close:
            continue
        pct = abs(close - open_) / open_
        if pct >= PRICE_SHOCK_PCT:
            symbol = event.payload.get("symbol") or str(event.payload.get("instrument_id", "?"))
            signals.append(
                MacroSignal(
                    kind="price_shock",
                    description=f"CME Globex {symbol} moved {pct:.1%} intraday (open {open_:.2f} -> close {close:.2f})",
                    source_reference=f"telemetry_event:{event.id} [cme_globex] {event.title}",
                )
            )

    # GDELT's query (see telemetry/gdelt.py::COMMODITY_SHOCK_QUERY) is
    # already scoped to shock-relevant language -- "commodities crash",
    # "grain shortage", "export ban", theme:ECON_COMMODITY -- so any fresh
    # match IS the signal; no extra magnitude filter needed on top of it.
    for event in _fresh_events(db, TelemetrySource.GDELT, limit=10):
        signals.append(
            MacroSignal(
                kind="news_shock",
                description=f"GDELT commodity-shock coverage: {event.title}",
                source_reference=f"telemetry_event:{event.id} [gdelt] {event.title}",
            )
        )

    # GFW 4Wings' raw cell schema isn't confirmed against live docs (see
    # telemetry/gfw_4wings.py) -- rather than invent an unverified magnitude
    # threshold on an unknown field, presence of a fresh cell is treated as
    # the ocean-side signal. Swap for a real magnitude threshold once the
    # live response schema is confirmed.
    for event in _fresh_events(db, TelemetrySource.GFW_4WINGS, limit=10):
        signals.append(
            MacroSignal(
                kind="ocean_anomaly",
                description=f"GFW 4Wings vessel-traffic anomaly: {event.title}",
                source_reference=f"telemetry_event:{event.id} [gfw_4wings] {event.title}",
            )
        )

    return signals


def _stage2_identify_companies(db: Session) -> list[tuple[str, str, str]]:
    """Turns an active macro signal into actual company names, via the
    trade/logistics-data sources that carry a real business identity
    (Import Genius customs manifests, Regrid parcel ownership). SeaVantage/
    Datalastic/VesselFinder/USACE don't carry a company name (they're
    vessel- or lock-level), so they can't identify a candidate on their
    own -- they corroborate one, attached as supporting evidence in the
    notes field of every candidate this stage produces.

    Returns (company_name, source_reference, corroboration_note) tuples.
    """
    corroboration_events: list[TelemetryEvent] = []
    for source in (
        TelemetrySource.SEAVANTAGE,
        TelemetrySource.DATALASTIC,
        TelemetrySource.VESSELFINDER,
        TelemetrySource.USACE,
    ):
        corroboration_events += _fresh_events(db, source, limit=10)

    if corroboration_events:
        sources_seen = sorted({e.source.value for e in corroboration_events})
        corroboration_note = f"{len(corroboration_events)} corroborating vessel/barge signals this sweep ({', '.join(sources_seen)})"
    else:
        corroboration_note = "no corroborating vessel/barge logistics signal this sweep"

    identified: list[tuple[str, str, str]] = []

    for event in _fresh_events(db, TelemetrySource.IMPORT_GENIUS, limit=25):
        name = event.payload.get("consignee_name") or event.payload.get("company_name")
        if name:
            identified.append((str(name), f"telemetry_event:{event.id} [import_genius] {event.title}", corroboration_note))

    for event in _fresh_events(db, TelemetrySource.REGRID, limit=25):
        properties = event.payload.get("properties") or {}
        owner = properties.get("owner") or properties.get("owner1")
        if owner:
            identified.append((str(owner), f"telemetry_event:{event.id} [regrid] {event.title}", corroboration_note))

    return identified


def _stage3_lending_appetite(db: Session, company_name: str) -> tuple[float, bool, str]:
    """Best-effort case-insensitive name match against fresh Cobalt
    Intelligence SOS records -- there's no shared entity ID between
    providers, so this is a name match, not a guaranteed join. Returns
    (score_delta, dismiss, note)."""
    needle = company_name.strip().lower()
    if not needle:
        return 0.0, False, "lending-appetite check skipped -- no company name to match"

    for event in _fresh_events(db, TelemetrySource.COBALT_INTELLIGENCE, limit=100):
        matched_name = str(event.payload.get("name", "")).strip().lower()
        if not matched_name or (needle not in matched_name and matched_name not in needle):
            continue

        status = str(event.payload.get("status") or event.payload.get("standing") or "").lower()
        display_name = event.payload.get("name")
        if any(k in status for k in _DISTRESS_STATUS_KEYWORDS):
            return -25.0, True, f"Cobalt Intelligence: '{display_name}' status '{status}' -- not fundable, dismissed"
        if any(k in status for k in _GOOD_STANDING_KEYWORDS):
            return 25.0, False, f"Cobalt Intelligence: '{display_name}' status '{status}' -- lending appetite confirmed"
        return 5.0, False, f"Cobalt Intelligence: matched '{display_name}', standing unconfirmed"

    return 0.0, False, (
        "lending-appetite check pending -- no Cobalt Intelligence match yet "
        "(configure MONITORED_SEARCHES in telemetry/cobalt_intelligence.py)"
    )


def _stage4_contact_lookup(db: Session, company_name: str) -> tuple[str | None, str]:
    """Best-effort name match against fresh Apollo.io org-search events.
    Interzoid has no bulk/macro-sweep counterpart (it's a per-entity
    lookup only, see enrichment/interzoid.py) so it isn't available at
    this stage -- it still runs once a candidate converts to a lead, via
    the normal per-lead Enrichment flow. Returns (phone, note)."""
    needle = company_name.strip().lower()
    if not needle:
        return None, "contact enrichment skipped -- no company name to match"

    for event in _fresh_events(db, TelemetrySource.APOLLO, limit=100):
        matched_name = str(event.payload.get("name", "")).strip().lower()
        if not matched_name or (needle not in matched_name and matched_name not in needle):
            continue
        phone_field = event.payload.get("primary_phone")
        phone = phone_field.get("number") if isinstance(phone_field, dict) else event.payload.get("phone")
        return phone, f"Apollo.io org match: '{event.payload.get('name')}'"

    return None, (
        "contact enrichment pending -- no Apollo.io org match yet; "
        "run per-candidate Interzoid/Apollo lookup from the Enrichment page once converted to a lead"
    )


def run_cme_macro_funnel_waterfall(db: Session) -> dict:
    """The real 4-stage funnel. Assumes telemetry.all_adapters() has
    already been swept this run (see run_silo_leadgen below) so the
    TelemetryEvent table reflects current provider state."""
    signals = _stage1_detect_signals(db)
    if not signals:
        return {
            "signals_detected": 0,
            "signal_summary": "",
            "candidates_created": 0,
            "candidates_dismissed": 0,
            "detail": "no active macro signal this sweep -- waterfall gated shut, zero candidates derived",
        }

    signal_summary = "; ".join(f"[{s.kind}] {s.description}" for s in signals[:5])
    identified = _stage2_identify_companies(db)

    created = 0
    dismissed = 0
    for company_name, trade_source_ref, corroboration_note in identified:
        already = (
            db.query(SiloCandidate)
            .filter(SiloCandidate.silo == CME_MACRO_SILO, SiloCandidate.source_reference == trade_source_ref)
            .first()
        )
        if already:
            continue

        score_delta, dismiss, lending_note = _stage3_lending_appetite(db, company_name)
        status = SiloCandidateStatus.DISMISSED if dismiss else SiloCandidateStatus.PENDING

        phone, contact_note = (None, "contact enrichment skipped -- lending-appetite filter dismissed this candidate")
        if not dismiss:
            phone, contact_note = _stage4_contact_lookup(db, company_name)

        notes = " | ".join(
            filter(
                None,
                [
                    f"Gated by: {signal_summary}",
                    corroboration_note,
                    lending_note,
                    contact_note,
                ],
            )
        )

        db.add(
            SiloCandidate(
                silo=CME_MACRO_SILO,
                company_name=company_name,
                phone=phone,
                source_reference=trade_source_ref,
                notes=notes,
                score=50.0 + score_delta,
                status=status,
            )
        )
        created += 1
        if dismiss:
            dismissed += 1

    db.commit()
    return {
        "signals_detected": len(signals),
        "signal_summary": signal_summary,
        "candidates_created": created,
        "candidates_dismissed": dismissed,
        "detail": f"{len(signals)} active signal(s), {created} candidate(s) identified from trade data, {dismissed} dismissed on lending-appetite filter",
    }


def run_silo_leadgen(db: Session) -> dict:
    """Ingests fresh telemetry from every configured adapter, derives one
    SiloCandidate per (event, mapped silo) pair for the 8 flat-mapped
    silos, and runs the CME Macro Funnel waterfall separately. Returns a
    summary of events ingested per source and candidates created per silo."""
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

    waterfall_result = run_cme_macro_funnel_waterfall(db)
    candidates_by_silo[CME_MACRO_SILO.value] += waterfall_result["candidates_created"]

    return {
        "telemetry_ingested": ingested_by_source,
        "candidates_created": candidates_by_silo,
        "cme_macro_funnel_waterfall": waterfall_result,
    }
