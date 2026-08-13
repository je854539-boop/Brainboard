"""Automated silo lead-gen sweep: runs every configured macro telemetry
adapter, then derives SiloCandidate rows from what it just ingested.

Seven of the nine silos use a flat mapping (SOURCE_TO_SILOS below): one
telemetry event from a mapped source produces one candidate in that silo,
1:1, no cross-source logic. That is the right model when a single
provider's record IS the lead signal (e.g. a UCC filing, an SOS status
change, an openFDA recall).

**CME Macro Funnel and Agriculture & Grain Handling are NOT in
SOURCE_TO_SILOS** -- both are genuine sequential waterfalls, not flat
dumps, because in both cases a company name alone isn't a lead and a
price move alone isn't a lead:

  CME MACRO FUNNEL (run_cme_macro_funnel_waterfall) -- "Chicago
  commodities market swings and crashes":
    1. SIGNAL   -- CME Globex (price) + GDELT (news) + GFW 4Wings (ocean).
                   No active signal = the waterfall stops here.
    2. IDENTIFY -- Import Genius (customs manifests) + Regrid (parcel
                   data, address-matched -- see _identify_via_regrid),
                   corroborated by SeaVantage/Datalastic/VesselFinder/
                   USACE vessel & barge activity in the same window.
    3. FILTER   -- Cobalt Intelligence SOS standing dismisses companies
                   that aren't fundable regardless of signal strength.
    4. CONTACT  -- Apollo.io org search attaches phone/contact info to
                   whatever survives the filter.

  AGRICULTURE & GRAIN HANDLING (run_agriculture_grain_handling_waterfall)
  -- a narrower, more direct version of the same pattern. CME Globex's
  tracked contracts are ALREADY scoped to just the CBOT grain/oilseed
  complex (see telemetry/cme_globex.py::SYMBOLS), and USACE's monitored
  locks default to the Mississippi/Illinois grain corridor -- so for this
  silo specifically, both are direct indicators, not just leading ones,
  and river-port disruption is exactly the kind of Midwest-specific
  signal a CME-only view would miss:
    1. SIGNAL   -- CME Globex grain-price shock + USACE grain-corridor
                   disruption (gate-change activity or barge queuing).
    2. IDENTIFY -- Regrid parcel data, address-matched (see below).

Every candidate either waterfall produces carries in its `notes` field
exactly which signal gated it in and which trade-data source identified
it -- nothing here is a black box; every row traces back to the real
telemetry that produced it via source_reference, same guarantee the flat
mapping gives the other 7 silos.
"""

import re
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
    TelemetrySource.UCC_FILINGS: [
        SiloName.FOOD_PROCESSING_FABRICATION_SILO,
        SiloName.HEAVY_MACHINERY_INDUSTRIAL_EQUIPMENT_SILO,
    ],
    TelemetrySource.SOS_REGISTRIES: [SiloName.HEALTHCARE_PHARMA_SILO, SiloName.ECOMMERCE_FULFILLMENT_SILO],
    TelemetrySource.HIGHERGOV: [SiloName.HIGHERGOV_FUNNEL],
    # A recall often means a business needs financing to cover remediation,
    # inventory write-off, or legal costs -- a genuine lead-gen signal.
    TelemetrySource.OPENFDA: [SiloName.HEALTHCARE_PHARMA_SILO],
    # GDELT, GFW_4WINGS, COBALT_INTELLIGENCE, APOLLO, USACE, and REGRID are
    # deliberately absent from this flat mapping. GDELT/GFW/Cobalt/Apollo
    # feed ONLY the CME Macro Funnel waterfall below. USACE feeds both
    # waterfalls. Regrid is NOT flat-mapped at all anymore -- a parcel's
    # owner-of-record isn't necessarily the business operating there (a
    # tenant doesn't own the land it leases), so Regrid identification is
    # always done via _identify_via_regrid()'s address-match logic inside
    # a waterfall, never by blindly trusting the owner field 1:1.
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


@dataclass
class MacroSignal:
    kind: str
    description: str
    source_reference: str


# ---------------------------------------------------------------------------
# Address matching -- shared by both waterfalls' Regrid identification step
# ---------------------------------------------------------------------------

# Field names aren't confirmed against live docs for either provider (see
# telemetry/import_genius.py, telemetry/cobalt_intelligence.py) -- this
# defensive multi-key lookup checks every plausible variant rather than
# guessing one and silently missing real matches.
_ADDRESS_KEYS = ("address", "business_address", "consignee_address", "mailing_address", "principal_address", "site_address")

_STREET_SUFFIXES = {
    "street": "st", "avenue": "ave", "boulevard": "blvd", "drive": "dr", "road": "rd",
    "lane": "ln", "suite": "ste", "court": "ct", "highway": "hwy", "parkway": "pkwy",
}


def _extract_address(payload: dict) -> str | None:
    for key in _ADDRESS_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _normalize_address(address: str) -> str:
    """Loose normalization for matching the same physical address written
    two different ways across two unrelated providers -- lowercase, strip
    punctuation, collapse common street-suffix spelling variants. Not a
    geocoder; this is a same-day textual match, not a certified address
    match."""
    cleaned = re.sub(r"[^\w\s]", " ", address.lower())
    words = [_STREET_SUFFIXES.get(w, w) for w in cleaned.split()]
    return " ".join(words)


def _identify_via_regrid(db: Session) -> list[tuple[str, str, str]]:
    """A Regrid parcel's owner-of-record is a land-ownership fact, not a
    confirmation that the owner is the business actually operating there
    -- an operator can lease land it doesn't own. So identification here
    is address-first: match the parcel's site address against a business
    address on file from Import Genius (when its payload carries one),
    and surface THAT company, not the landowner. When no address match is
    found, the parcel owner is still surfaced -- flagged explicitly in
    notes as land-ownership-only, unconfirmed-operator -- rather than
    silently dropped, since it may still be the right business, just not
    address-confirmed.

    Returns (company_name, source_reference, note) tuples.
    """
    known_addresses: dict[str, str] = {}
    for event in _fresh_events(db, TelemetrySource.IMPORT_GENIUS, limit=25):
        name = event.payload.get("consignee_name") or event.payload.get("company_name")
        address = _extract_address(event.payload)
        if name and address:
            known_addresses[_normalize_address(str(address))] = str(name)

    identified: list[tuple[str, str, str]] = []
    for event in _fresh_events(db, TelemetrySource.REGRID, limit=25):
        properties = event.payload.get("properties") or {}
        parcel_address = properties.get("address")
        owner = properties.get("owner") or properties.get("owner1")
        if not parcel_address and not owner:
            continue

        source_reference = f"telemetry_event:{event.id} [regrid] {event.title}"
        matched_company = known_addresses.get(_normalize_address(str(parcel_address))) if parcel_address else None

        if matched_company:
            identified.append((
                matched_company,
                source_reference,
                f"Regrid parcel at '{parcel_address}' address-matches Import Genius's business address for '{matched_company}'",
            ))
        elif owner:
            identified.append((
                str(owner),
                source_reference,
                f"Regrid parcel owner '{owner}' at '{parcel_address or 'unknown address'}' -- "
                "land ownership only, operating business at this address not independently confirmed",
            ))

    return identified


# ---------------------------------------------------------------------------
# Shared signal detectors
# ---------------------------------------------------------------------------

# A same-day |close-open| move of this size on any tracked CBOT
# grain/oilseed contract counts as an active price-shock signal.
PRICE_SHOCK_PCT = 0.02

_DISTRESS_STATUS_KEYWORDS = ("suspended", "dissolved", "revoked", "inactive", "delinquent", "forfeited")
_GOOD_STANDING_KEYWORDS = ("active", "good standing", "in good standing", "current")


def _cme_price_shock_signals(db: Session) -> list[MacroSignal]:
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
    return signals


def _gdelt_shock_signals(db: Session) -> list[MacroSignal]:
    # GDELT's query (see telemetry/gdelt.py::COMMODITY_SHOCK_QUERY) is
    # already scoped to shock-relevant language -- "commodities crash",
    # "grain shortage", "export ban", theme:ECON_COMMODITY -- so any fresh
    # match IS the signal; no extra magnitude filter needed on top of it.
    return [
        MacroSignal(
            kind="news_shock",
            description=f"GDELT commodity-shock coverage: {event.title}",
            source_reference=f"telemetry_event:{event.id} [gdelt] {event.title}",
        )
        for event in _fresh_events(db, TelemetrySource.GDELT, limit=10)
    ]


def _gfw_anomaly_signals(db: Session) -> list[MacroSignal]:
    # GFW 4Wings' raw cell schema isn't confirmed against live docs (see
    # telemetry/gfw_4wings.py) -- rather than invent an unverified
    # magnitude threshold on an unknown field, presence of a fresh cell is
    # treated as the ocean-side signal. Swap for a real magnitude
    # threshold once the live response schema is confirmed.
    return [
        MacroSignal(
            kind="ocean_anomaly",
            description=f"GFW 4Wings vessel-traffic anomaly: {event.title}",
            source_reference=f"telemetry_event:{event.id} [gfw_4wings] {event.title}",
        )
        for event in _fresh_events(db, TelemetrySource.GFW_4WINGS, limit=10)
    ]


def _usace_disruption_signals(db: Session) -> list[MacroSignal]:
    # Every USACE event (see telemetry/usace.py) is already grain-corridor
    # specific by construction -- MONITORED_LOCKS defaults to the
    # Mississippi/Illinois Waterway, and both its channels (CWMS
    # gate-change activity, AIS barge queuing) only emit a record once
    # they've crossed a real disruption threshold (GATE_CHANGE_THRESHOLD /
    # QUEUE_THRESHOLD). So any fresh USACE event IS a grain-corridor
    # congestion signal, not just a raw data point that needs re-filtering.
    return [
        MacroSignal(
            kind="river_corridor_disruption",
            description=f"USACE grain-corridor disruption: {event.title}",
            source_reference=f"telemetry_event:{event.id} [usace] {event.title}",
        )
        for event in _fresh_events(db, TelemetrySource.USACE, limit=20)
    ]


# ---------------------------------------------------------------------------
# CME Macro Funnel -- 4-stage sequential waterfall
# ---------------------------------------------------------------------------

CME_MACRO_SILO = SiloName.CME_MACRO_FUNNEL


def _stage1_detect_signals(db: Session) -> list[MacroSignal]:
    """The gate. Returns every active macro signal found across the three
    leading-indicator channels. An empty return means the waterfall stops
    here -- stages 2-4 never run and zero candidates get created."""
    return _cme_price_shock_signals(db) + _gdelt_shock_signals(db) + _gfw_anomaly_signals(db)


def _stage2_identify_companies(db: Session) -> list[tuple[str, str, str]]:
    """Turns an active macro signal into actual company names, via the
    trade/logistics-data sources that carry a real business identity
    (Import Genius customs manifests, Regrid parcel data -- address-
    matched, see _identify_via_regrid). SeaVantage/Datalastic/
    VesselFinder/USACE don't carry a company name (they're vessel- or
    lock-level), so they can't identify a candidate on their own -- they
    corroborate one, attached as supporting evidence in the notes field
    of every candidate this stage produces.

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

    for company_name, source_reference, regrid_note in _identify_via_regrid(db):
        identified.append((company_name, source_reference, f"{corroboration_note} | {regrid_note}"))

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


# ---------------------------------------------------------------------------
# Agriculture & Grain Handling -- 2-stage waterfall
# ---------------------------------------------------------------------------

GRAIN_SILO = SiloName.AGRICULTURE_GRAIN_HANDLING_SILO


def run_agriculture_grain_handling_waterfall(db: Session) -> dict:
    """A narrower, more direct version of the CME Macro Funnel pattern.
    CME Globex's tracked symbols are already scoped to just the CBOT
    grain/oilseed complex (corn/soybeans/wheat/meal/oil -- see
    telemetry/cme_globex.py::SYMBOLS) and USACE's monitored locks default
    to the Mississippi/Illinois grain corridor, so for THIS silo both are
    direct operating-condition indicators for grain handlers, not just
    leading indicators of a broader macro trend the way they are for CME
    Macro Funnel. No lending-appetite/contact stages here -- those still
    run once a candidate converts to a lead via the normal Enrichment
    flow; keeping this waterfall to signal+identify matches what it's
    actually gating (grain-market/river-corridor conditions -> real
    grain-handling businesses at the physical location), not a broader
    creditworthiness funnel.
    """
    signals = _cme_price_shock_signals(db) + _usace_disruption_signals(db)
    if not signals:
        return {
            "signals_detected": 0,
            "signal_summary": "",
            "candidates_created": 0,
            "detail": "no active grain-price or river-corridor signal this sweep -- waterfall gated shut, zero candidates derived",
        }

    signal_summary = "; ".join(f"[{s.kind}] {s.description}" for s in signals[:5])

    created = 0
    for company_name, source_reference, regrid_note in _identify_via_regrid(db):
        already = (
            db.query(SiloCandidate)
            .filter(SiloCandidate.silo == GRAIN_SILO, SiloCandidate.source_reference == source_reference)
            .first()
        )
        if already:
            continue

        db.add(
            SiloCandidate(
                silo=GRAIN_SILO,
                company_name=company_name,
                source_reference=source_reference,
                notes=f"Gated by: {signal_summary} | {regrid_note}",
            )
        )
        created += 1

    db.commit()
    return {
        "signals_detected": len(signals),
        "signal_summary": signal_summary,
        "candidates_created": created,
        "detail": f"{len(signals)} active grain-price/river-corridor signal(s), {created} candidate(s) identified via Regrid",
    }


def run_silo_leadgen(db: Session) -> dict:
    """Ingests fresh telemetry from every configured adapter, derives one
    SiloCandidate per (event, mapped silo) pair for the 7 flat-mapped
    silos, and runs the CME Macro Funnel and Agriculture & Grain Handling
    waterfalls separately. Returns a summary of events ingested per
    source and candidates created per silo."""
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

    cme_result = run_cme_macro_funnel_waterfall(db)
    candidates_by_silo[CME_MACRO_SILO.value] += cme_result["candidates_created"]

    grain_result = run_agriculture_grain_handling_waterfall(db)
    candidates_by_silo[GRAIN_SILO.value] += grain_result["candidates_created"]

    return {
        "telemetry_ingested": ingested_by_source,
        "candidates_created": candidates_by_silo,
        "cme_macro_funnel_waterfall": cme_result,
        "agriculture_grain_handling_waterfall": grain_result,
    }
