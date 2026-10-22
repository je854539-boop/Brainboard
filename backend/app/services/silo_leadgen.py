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
    1. SIGNAL   -- CME Globex (price) + GDELT (news, now also tariff/
                   trade-policy coverage) + GFW 4Wings (ocean) + USACE
                   (river/port disruption, every monitored corridor --
                   grain, steel/coal, petrochemical, port congestion; see
                   telemetry/usace.py). No active signal = the waterfall
                   stops here.
    2. IDENTIFY -- Import Genius (customs manifests) + Regrid (parcel
                   data, address-matched -- see _identify_via_regrid),
                   corroborated by SeaVantage/Datalastic/VesselFinder/
                   USACE vessel & barge activity in the same window.
    3. FILTER   -- two independent dismiss checks, either one kills the
                   candidate: (a) Cobalt Intelligence SOS standing --
                   not fundable regardless of signal strength; (b)
                   revenue range (_stage3b_revenue_range_filter) --
                   Apollo.io org-search revenue outside
                   settings.mca_target_revenue_min_usd/max_usd (default
                   $5M-$49M, the deal sizes actually worth pursuing).
                   Neither check dismisses for lack of data -- only a
                   confirmed disqualifying figure does.
    4. CONTACT  -- Apollo.io org search (primary) + Cobalt Intelligence
                   SOS records (checked defensively -- it's a standing
                   check, not fundamentally a contact database, but used
                   if it happens to carry one) attach phone/email to
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

Cross-provider name matching (stage 3's Cobalt Intelligence match, stage
4's Apollo match) goes through app/services/entity_matching.py, which
uses Interzoid's fuzzy company-name matching when INTERZOID_API_KEY is
configured -- catching legal-name variants (DBAs, LLC/Inc suffixes,
punctuation) that a raw substring check misses -- and falls back to
substring matching otherwise, same behavior as before Interzoid was
wired in for this purpose.
"""

import math
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import SiloCandidateStatus, SiloName, TelemetrySource
from app.models.orm import SiloCandidate, TelemetryEvent
from app.services import entity_matching, telemetry

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


_ORIGIN_SOURCE_TAG_RE = re.compile(r"\[([a-z0-9_]+)\]")


def _infer_origin_source(source_reference: str | None) -> TelemetrySource | None:
    """Every source_reference this module writes already embeds a
    "[provider]" tag (see the flat-mapped path, _identify_via_regrid, and
    _stage2_identify_companies above -- e.g. "telemetry_event:123
    [import_genius] ..."). Parsed once here, at candidate-creation time,
    into the real origin_source column instead of leaving
    conversion-rate-by-source analytics dependent on re-parsing free text
    on every query. Returns None (not a guess) if no recognized tag is
    found -- shouldn't happen given every call site sets one, but this
    function doesn't invent an answer if that invariant ever breaks."""
    if not source_reference:
        return None
    match = _ORIGIN_SOURCE_TAG_RE.search(source_reference)
    if not match:
        return None
    try:
        return TelemetrySource(match.group(1))
    except ValueError:
        return None


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
    # Only USACE-origin signals carry these (see _usace_disruption_signals)
    # -- a CME price shock or GDELT headline has no specific place to
    # point Stage 2's identification search at. None for every other
    # signal kind; _geo_targets_from_signals skips those.
    commodity: str | None = None
    coordinates: tuple[float, float] | None = None


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


def _regrid_geometry_centroid(payload: dict) -> tuple[float, float] | None:
    """Real Regrid API responses are GeoJSON Features -- payload["geometry"]
    carries the parcel's actual boundary (see regrid.py's fetch(), which
    passes the raw feature through unmodified). Returns an approximate
    centroid (average of ring vertices for a Polygon/MultiPolygon, direct
    coordinates for a Point) as (lat, lon) -- not a true area-weighted
    centroid, close enough for globe placement. Returns None rather than
    guessing when the geometry is missing or an unrecognized type, same
    posture as everywhere else geo data is either real or absent, never
    fabricated."""
    geometry = payload.get("geometry")
    if not isinstance(geometry, dict):
        return None
    gtype, coords = geometry.get("type"), geometry.get("coordinates")
    if not coords:
        return None
    try:
        if gtype == "Point":
            lon, lat = coords[0], coords[1]
            return (float(lat), float(lon))
        if gtype == "Polygon":
            ring = coords[0]
        elif gtype == "MultiPolygon":
            ring = coords[0][0]
        else:
            return None
        lons = [float(pt[0]) for pt in ring]
        lats = [float(pt[1]) for pt in ring]
        return (sum(lats) / len(lats), sum(lons) / len(lons))
    except (TypeError, IndexError, ValueError, ZeroDivisionError):
        return None


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance -- good enough for "is this parcel near this
    lock/port" at the 10-60 mile scale these searches use; no need for a
    geodesic library over that range."""
    earth_radius_mi = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * earth_radius_mi * math.asin(math.sqrt(a))


# How far from an active disruption a parcel still counts as plausibly
# affected by it. Waterside industrial facilities (grain elevators, ore
# docks, petrochemical plants) are sited directly on the corridor, so a
# tight radius is right for them. Port congestion is different -- the
# businesses actually feeling a port delay are spread across its whole
# trucking catchment area, not clustered at the dock -- so that radius
# is deliberately much wider. Tunable policy, not a measured constant.
_COMMODITY_SEARCH_RADIUS_MILES: dict[str, float] = {
    "grain": 20.0,
    "steel_coal": 20.0,
    "petrochemical": 20.0,
    "port_congestion": 60.0,
}
_DEFAULT_SEARCH_RADIUS_MILES = 25.0

# Regrid's actual land-use/zoning field name isn't confirmed against
# live docs (same posture as _ADDRESS_KEYS above) -- check every
# plausible variant rather than guessing one and silently missing a real
# classification.
_LAND_USE_KEYS = ("land_use", "landuse", "usedesc", "use_description", "zoning_description", "zoning")

# Keyword match against whatever land-use/zoning text is found, per
# commodity -- confirms a parcel is actually the right KIND of facility,
# not just physically near the disruption. port_congestion has no single
# matching industry (any import-dependent business in the catchment area
# is a fair candidate), so its keywords are a bonus-confidence signal,
# never a requirement -- see _nearest_geo_target's caller, which never
# dismisses a radius match for lack of a land-use field.
_COMMODITY_LAND_USE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "grain": ("grain", "elevator", "agricultural", "farm", "silo"),
    "steel_coal": ("steel", "iron", "ore", "coal", "metal", "foundry", "mill", "smelt"),
    "petrochemical": ("petrochemical", "chemical", "refinery", "petroleum", "plastics"),
    "port_congestion": ("warehouse", "distribution", "logistics", "fulfillment", "freight", "terminal"),
}


def _extract_land_use(properties: dict) -> str | None:
    for key in _LAND_USE_KEYS:
        value = properties.get(key)
        if value:
            return str(value).lower()
    return None


@dataclass
class GeoTarget:
    lat: float
    lon: float
    commodity: str
    label: str
    radius_mi: float


def _geo_targets_from_signals(signals: list[MacroSignal]) -> list[GeoTarget]:
    """One search target per active signal that actually carries a
    location and a commodity (today, only USACE-origin ones do -- a CME
    price shock or a GDELT tariff headline has no specific place to
    point the search at, so those are simply skipped here, not an
    error). This is the piece that turns "something disrupted
    somewhere" into "go look specifically here, for this kind of
    business."
    """
    targets = []
    for signal in signals:
        if signal.commodity and signal.coordinates:
            lat, lon = signal.coordinates
            radius = _COMMODITY_SEARCH_RADIUS_MILES.get(signal.commodity, _DEFAULT_SEARCH_RADIUS_MILES)
            targets.append(GeoTarget(lat=lat, lon=lon, commodity=signal.commodity, label=signal.description, radius_mi=radius))
    return targets


def _nearest_geo_target(coordinates: tuple[float, float] | None, targets: list[GeoTarget]) -> tuple[GeoTarget, float] | None:
    """The closest target a parcel falls within the radius of, or None if
    it's outside every active target's radius (or there are no active
    geo-tagged signals this sweep, or the parcel has no coordinates)."""
    if not coordinates or not targets:
        return None
    lat, lon = coordinates
    best: tuple[GeoTarget, float] | None = None
    for target in targets:
        distance = _haversine_miles(lat, lon, target.lat, target.lon)
        if distance <= target.radius_mi and (best is None or distance < best[1]):
            best = (target, distance)
    return best


def _geo_match_note(properties: dict, coordinates: tuple[float, float] | None, targets: list[GeoTarget]) -> str:
    """Returns a note prefix describing whether this parcel sits inside
    an active disruption's search radius, and whether its land-use/
    zoning text (when available) actually confirms the matching
    industry. Empty string when there's no active geo target this
    sweep, or the parcel falls outside all of them -- NOT dismissed in
    either case, since the existing address-match path may still be
    right; this only ever adds confidence, never removes a candidate."""
    match = _nearest_geo_target(coordinates, targets)
    if not match:
        return ""
    target, distance = match
    land_use = _extract_land_use(properties)
    keywords = _COMMODITY_LAND_USE_KEYWORDS.get(target.commodity, ())
    if land_use and keywords and any(k in land_use for k in keywords):
        return (
            f"TARGETED MATCH: {distance:.1f}mi from active {target.commodity} signal ({target.label}), "
            f"land-use '{land_use}' confirms {target.commodity} classification -- "
        )
    if land_use:
        return (
            f"Near active {target.commodity} signal ({target.label}, {distance:.1f}mi) but land-use "
            f"'{land_use}' doesn't confirm {target.commodity} -- radius match only, not industry-confirmed -- "
        )
    return (
        f"Near active {target.commodity} signal ({target.label}, {distance:.1f}mi); land-use classification "
        f"unavailable to confirm -- radius match only, not industry-confirmed -- "
    )


def _identify_via_regrid(
    db: Session, targets: list[GeoTarget] | None = None
) -> list[tuple[str, str, str, tuple[float, float] | None]]:
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

    `targets` (see _geo_targets_from_signals) narrows WHERE and WHAT KIND
    of business this search is actually looking for -- when an active
    disruption signal carries both a location and a commodity, a parcel
    inside that target's radius gets its note prefixed with how strong
    the match is (land-use-confirmed vs. radius-only vs. unconfirmed --
    see _geo_match_note). This never dismisses a candidate for falling
    outside every target's radius, or for there being no active geo
    target at all -- the existing address-match result stands either
    way, just without the extra confidence prefix.

    Returns (company_name, source_reference, note, coordinates) tuples --
    coordinates is (lat, lon) from the parcel's own GeoJSON geometry when
    present, else None (see _regrid_geometry_centroid). Powers the
    globe's silo-funnel-lifecycle layer.
    """
    targets = targets or []
    known_addresses: dict[str, str] = {}
    for event in _fresh_events(db, TelemetrySource.IMPORT_GENIUS, limit=25):
        name = event.payload.get("consignee_name") or event.payload.get("company_name")
        address = _extract_address(event.payload)
        if name and address:
            known_addresses[_normalize_address(str(address))] = str(name)

    identified: list[tuple[str, str, str, tuple[float, float] | None]] = []
    for event in _fresh_events(db, TelemetrySource.REGRID, limit=25):
        properties = event.payload.get("properties") or {}
        parcel_address = properties.get("address")
        owner = properties.get("owner") or properties.get("owner1")
        if not parcel_address and not owner:
            continue

        source_reference = f"telemetry_event:{event.id} [regrid] {event.title}"
        matched_company = known_addresses.get(_normalize_address(str(parcel_address))) if parcel_address else None
        coordinates = _regrid_geometry_centroid(event.payload)
        geo_note = _geo_match_note(properties, coordinates, targets)

        if matched_company:
            identified.append((
                matched_company,
                source_reference,
                f"{geo_note}Regrid parcel at '{parcel_address}' address-matches Import Genius's business address for '{matched_company}'",
                coordinates,
            ))
        elif owner:
            identified.append((
                str(owner),
                source_reference,
                f"{geo_note}Regrid parcel owner '{owner}' at '{parcel_address or 'unknown address'}' -- "
                "land ownership only, operating business at this address not independently confirmed",
                coordinates,
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
    # "grain shortage", "export ban", theme:ECON_COMMODITY -- AND
    # tariff/trade-policy language ("tariff", "trade war", Section
    # 301/232) -- so any fresh match IS the signal; no extra magnitude
    # filter needed on top of it.
    return [
        MacroSignal(
            kind="news_shock",
            description=f"GDELT commodity/tariff-shock coverage: {event.title}",
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


def _usace_disruption_signals(db: Session, commodities: set[str] | None = None) -> list[MacroSignal]:
    """Every USACE event (see telemetry/usace.py) already crossed a real
    disruption threshold before it was recorded (GATE_CHANGE_THRESHOLD /
    QUEUE_THRESHOLD) -- any fresh event IS a disruption signal, not a raw
    data point needing re-filtering. But MONITORED_LOCKS now spans
    several corridors (grain, steel_coal, petrochemical, port_congestion
    -- see that file), so a caller MUST say which commodity tag(s) it
    actually wants: the Agriculture & Grain Handling waterfall passes
    {"grain"} so a steel-corridor or port-congestion event can't gate it
    open by mistake, while the CME Macro Funnel (broader, cross-industry)
    passes None for every corridor. Each event's commodity tag was
    written into its payload by usace.py's fetch() -- read back here
    rather than re-deriving it from the lock name."""
    signals = []
    for event in _fresh_events(db, TelemetrySource.USACE, limit=20):
        commodity = event.payload.get("commodity")
        if commodities is not None and commodity not in commodities:
            continue
        label = (commodity or "river_corridor").replace("_", " ")
        lat, lon = event.payload.get("lat"), event.payload.get("lon")
        coordinates = (float(lat), float(lon)) if lat is not None and lon is not None else None
        signals.append(
            MacroSignal(
                kind="river_corridor_disruption",
                description=f"USACE {label} disruption: {event.title}",
                source_reference=f"telemetry_event:{event.id} [usace] {event.title}",
                commodity=commodity,
                coordinates=coordinates,
            )
        )
    return signals


# ---------------------------------------------------------------------------
# CME Macro Funnel -- 4-stage sequential waterfall
# ---------------------------------------------------------------------------

CME_MACRO_SILO = SiloName.CME_MACRO_FUNNEL


def _stage1_detect_signals(db: Session) -> list[MacroSignal]:
    """The gate. Returns every active macro signal found across the four
    leading-indicator channels (CME Globex price shock, GDELT news --
    now also tariff/trade-policy coverage, see telemetry/gdelt.py --, GFW
    ocean anomaly, and USACE river/port disruption across EVERY monitored
    corridor, not just grain -- a steel-corridor or port-congestion event
    can open this funnel same as a grain one, since this waterfall's
    filter/contact stages are generic, not grain-specific). An empty
    return means the waterfall stops here -- stages 2-4 never run and
    zero candidates get created."""
    return (
        _cme_price_shock_signals(db)
        + _gdelt_shock_signals(db)
        + _gfw_anomaly_signals(db)
        + _usace_disruption_signals(db)
    )


def _stage2_identify_companies(db: Session, targets: list[GeoTarget] | None = None) -> list[tuple[str, str, str]]:
    """Turns an active macro signal into actual company names, via the
    trade/logistics-data sources that carry a real business identity
    (Import Genius customs manifests, Regrid parcel data -- address-
    matched AND, when `targets` carries active geo+commodity signals,
    location/industry-matched too, see _identify_via_regrid).
    SeaVantage/Datalastic/VesselFinder/USACE don't carry a company name
    (they're vessel- or lock-level), so they can't identify a candidate
    on their own -- they corroborate one, attached as supporting
    evidence in the notes field of every candidate this stage produces.

    Returns (company_name, source_reference, corroboration_note,
    coordinates) tuples -- coordinates is None for Import Genius-sourced
    identifications (that provider's payload carries no confirmed geo
    field, see telemetry/import_genius.py), populated for Regrid-sourced
    ones from the parcel's own geometry.
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

    identified: list[tuple[str, str, str, tuple[float, float] | None]] = []

    for event in _fresh_events(db, TelemetrySource.IMPORT_GENIUS, limit=25):
        name = event.payload.get("consignee_name") or event.payload.get("company_name")
        if name:
            identified.append((str(name), f"telemetry_event:{event.id} [import_genius] {event.title}", corroboration_note, None))

    for company_name, source_reference, regrid_note, coordinates in _identify_via_regrid(db, targets=targets):
        identified.append((company_name, source_reference, f"{corroboration_note} | {regrid_note}", coordinates))

    return identified


def _stage3_lending_appetite(db: Session, company_name: str) -> tuple[float, bool, str]:
    """Name match against fresh Cobalt Intelligence SOS records via
    entity_matching.names_match -- Interzoid fuzzy scoring when
    configured (catches DBAs/suffix variants a raw substring check
    misses), falling back to substring containment otherwise. There's no
    shared entity ID between providers, so this is always a name match,
    not a guaranteed join. Returns (score_delta, dismiss, note)."""
    if not company_name.strip():
        return 0.0, False, "lending-appetite check skipped -- no company name to match"

    interzoid_key = get_settings().interzoid_api_key
    for event in _fresh_events(db, TelemetrySource.COBALT_INTELLIGENCE, limit=100):
        matched_name = event.payload.get("name")
        if not entity_matching.names_match(interzoid_key, company_name, matched_name):
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


_REVENUE_KEYS = ("annual_revenue", "organization_revenue", "estimated_annual_revenue", "revenue")


def _extract_revenue(payload: dict) -> float | None:
    """Defensive multi-key extraction, same posture as
    _extract_contact_fields below -- checks several plausible Apollo.io
    organization-search field names rather than assuming one. HONESTY
    NOTE: Apollo's org-search schema (like its exact endpoint, see
    telemetry/apollo.py) isn't verifiable live from this sandbox; these
    are the field names documented across Apollo's API versions, not a
    live-confirmed single source of truth. A nested {"organization": {...}}
    wrapper is also checked in case the sweep's payload carries the
    revenue one level down rather than at the top."""
    candidates = [payload] + ([payload["organization"]] if isinstance(payload.get("organization"), dict) else [])
    for source in candidates:
        for key in _REVENUE_KEYS:
            value = source.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
    return None


def _stage3b_revenue_range_filter(db: Session, company_name: str) -> tuple[float, bool, str]:
    """MCA-fit deal-size filter -- settings.mca_target_revenue_min_usd/
    mca_target_revenue_max_usd (default $5M-$49M). Reuses the same fresh
    Apollo.io org-search telemetry stage 4 already pulls for contact
    data (no separate API call), name-matched the same way stage 3/4
    are. Never dismisses for lack of data -- only a CONFIRMED revenue
    figure outside the target band disqualifies a candidate; no match
    yet just means the band is unconfirmed, same honesty posture as
    stage 3's "pending" branch. Returns (score_delta, dismiss, note)."""
    if not company_name.strip():
        return 0.0, False, "revenue-range check skipped -- no company name to match"

    settings = get_settings()
    interzoid_key = settings.interzoid_api_key
    for event in _fresh_events(db, TelemetrySource.APOLLO, limit=100):
        matched_name = event.payload.get("name")
        if not entity_matching.names_match(interzoid_key, company_name, matched_name):
            continue

        revenue = _extract_revenue(event.payload)
        if revenue is None:
            return 0.0, False, f"Apollo.io matched '{matched_name}' but no revenue figure in payload -- band unconfirmed"

        if settings.mca_target_revenue_min_usd <= revenue <= settings.mca_target_revenue_max_usd:
            return 20.0, False, f"Apollo.io revenue ${revenue:,.0f} -- within target ${settings.mca_target_revenue_min_usd:,.0f}-${settings.mca_target_revenue_max_usd:,.0f} band"

        return -30.0, True, (
            f"Apollo.io revenue ${revenue:,.0f} -- outside target "
            f"${settings.mca_target_revenue_min_usd:,.0f}-${settings.mca_target_revenue_max_usd:,.0f} band, dismissed"
        )

    return 0.0, False, "revenue-range check pending -- no Apollo.io match yet"


_PHONE_KEYS = ("phone", "phone_number", "primary_phone", "company_phone", "registered_agent_phone")
_EMAIL_KEYS = ("email", "email_address", "company_email", "contact_email", "registered_agent_email")


def _extract_contact_fields(payload: dict) -> tuple[str | None, str | None]:
    """Defensive multi-key extraction, same posture as every other
    unconfirmed-schema field elsewhere in this project. Checks several
    plausible key names rather than assuming one and silently missing a
    real value; a nested {"number": ...} phone object (Apollo's
    documented primary_phone shape) is handled specially."""
    phone = None
    for key in _PHONE_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("number")
        if value:
            phone = str(value)
            break

    email = None
    for key in _EMAIL_KEYS:
        value = payload.get(key)
        if value:
            email = str(value)
            break

    return phone, email


def _stage4_contact_enrichment(db: Session, company_name: str) -> tuple[str | None, str | None, str]:
    """Contact enrichment against fresh Apollo.io org-search AND Cobalt
    Intelligence SOS events, name-matched via entity_matching.names_match
    (same utility as stage 3). Apollo is the primary source here -- its
    org-search payload documents a `primary_phone` field, though note
    this is company-level data (an org search), not a specific person's
    direct line; a true per-contact phone/email would need Apollo's
    People/Contact Search endpoint, which isn't wired here. Cobalt
    Intelligence's real product is SOS business-registry data (name,
    standing, registered agent) -- it isn't fundamentally a contact
    database the way Apollo is, so it's checked defensively for a
    phone/email field rather than assumed to always have one; when it
    does (e.g. a registered-agent contact), it's used too.

    Returns (phone, email, note)."""
    if not company_name.strip():
        return None, None, "contact enrichment skipped -- no company name to match"

    interzoid_key = get_settings().interzoid_api_key
    found_phone, found_email = None, None
    matched_sources = []

    for source in (TelemetrySource.APOLLO, TelemetrySource.COBALT_INTELLIGENCE):
        for event in _fresh_events(db, source, limit=100):
            matched_name = event.payload.get("name")
            if not entity_matching.names_match(interzoid_key, company_name, matched_name):
                continue
            phone, email = _extract_contact_fields(event.payload)
            if phone and not found_phone:
                found_phone = phone
            if email and not found_email:
                found_email = email
            if phone or email:
                matched_sources.append(f"{source.value}:'{event.payload.get('name')}'")
            break  # one match per source is enough

    if found_phone or found_email:
        return found_phone, found_email, f"Contact enrichment matched {', '.join(matched_sources)}"

    return None, None, (
        "contact enrichment pending -- no Apollo.io/Cobalt Intelligence match with a phone or email yet; "
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
    identified = _stage2_identify_companies(db, targets=_geo_targets_from_signals(signals))

    created = 0
    dismissed = 0
    for company_name, trade_source_ref, corroboration_note, coordinates in identified:
        already = (
            db.query(SiloCandidate)
            .filter(SiloCandidate.silo == CME_MACRO_SILO, SiloCandidate.source_reference == trade_source_ref)
            .first()
        )
        if already:
            continue

        standing_delta, standing_dismiss, lending_note = _stage3_lending_appetite(db, company_name)
        revenue_delta, revenue_dismiss, revenue_note = _stage3b_revenue_range_filter(db, company_name)
        score_delta = standing_delta + revenue_delta
        dismiss = standing_dismiss or revenue_dismiss
        status = SiloCandidateStatus.DISMISSED if dismiss else SiloCandidateStatus.PENDING

        phone, email, contact_note = (None, None, "contact enrichment skipped -- dismissed on standing or revenue-range filter")
        if not dismiss:
            phone, email, contact_note = _stage4_contact_enrichment(db, company_name)

        notes = " | ".join(
            filter(
                None,
                [
                    f"Gated by: {signal_summary}",
                    corroboration_note,
                    lending_note,
                    revenue_note,
                    contact_note,
                ],
            )
        )

        db.add(
            SiloCandidate(
                silo=CME_MACRO_SILO,
                company_name=company_name,
                phone=phone,
                email=email,
                source_reference=trade_source_ref,
                notes=notes,
                score=50.0 + score_delta,
                status=status,
                origin_source=_infer_origin_source(trade_source_ref),
                latitude=coordinates[0] if coordinates else None,
                longitude=coordinates[1] if coordinates else None,
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
    telemetry/cme_globex.py::SYMBOLS). USACE now monitors several
    corridors, not just grain (steel/coal, petrochemical, and coastal
    port congestion -- see telemetry/usace.py), so this waterfall calls
    _usace_disruption_signals(db, commodities={"grain"}) specifically --
    a steel-corridor or port-congestion disruption must NOT gate open a
    silo named Agriculture & Grain Handling, even though the same USACE
    adapter also feeds those other signals into the broader CME Macro
    Funnel above. For the grain corridor specifically, both CME Globex
    and USACE are direct operating-condition indicators for grain
    handlers, not just leading indicators of a broader macro trend the
    way they are for CME Macro Funnel. No lending-appetite/contact stages
    here -- those still run once a candidate converts to a lead via the
    normal Enrichment flow; keeping this waterfall to signal+identify
    matches what it's actually gating (grain-market/river-corridor
    conditions -> real grain-handling businesses at the physical
    location), not a broader creditworthiness funnel.
    """
    signals = _cme_price_shock_signals(db) + _usace_disruption_signals(db, commodities={"grain"})
    if not signals:
        return {
            "signals_detected": 0,
            "signal_summary": "",
            "candidates_created": 0,
            "detail": "no active grain-price or river-corridor signal this sweep -- waterfall gated shut, zero candidates derived",
        }

    signal_summary = "; ".join(f"[{s.kind}] {s.description}" for s in signals[:5])

    created = 0
    for company_name, source_reference, regrid_note, coordinates in _identify_via_regrid(
        db, targets=_geo_targets_from_signals(signals)
    ):
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
                origin_source=_infer_origin_source(source_reference),
                latitude=coordinates[0] if coordinates else None,
                longitude=coordinates[1] if coordinates else None,
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
                    origin_source=event.source,
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
