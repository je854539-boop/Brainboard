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

import httpx
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import SiloCandidateStatus, SiloName, TelemetrySource
from app.models.orm import SiloCandidate, TelemetryEvent
from app.services import entity_matching, telemetry
from app.services.telemetry.apollo import ApolloAdapter
from app.services.telemetry.cobalt_intelligence import CobaltIntelligenceAdapter
from app.services.telemetry.gdelt import GDELTAdapter
from app.services.telemetry.usace import MONITORED_LOCKS, vessels_near_datalastic, vessels_near_vesselfinder

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
    # Share of this corridor's traffic this commodity represents (see
    # usace.py's MONITORED_LOCKS commodities mix) -- 1.0 for every
    # non-USACE signal kind, where the concept doesn't apply.
    weight: float = 1.0
    # A real named city/state near the disruption (usace.py's
    # apollo_location) -- Apollo.io's org search takes a place name, not
    # a lat/lon radius, so this is what actually drives a disruption-
    # triggered Apollo hunt (see _stage2b_apollo_industry_hunt). None for
    # any signal kind without a specific place attached.
    location_hint: str | None = None
    # Plain 2-letter state code (usace.py's `state`) -- Cobalt
    # Intelligence's SOS search takes a state, not a city/place string.
    # See _stage2c_cobalt_industry_hunt.
    state: str | None = None
    # "expansion" (a producer riding a price windfall, or a consumer
    # getting relief) or "distress" (a producer getting squeezed, or a
    # consumer getting squeezed by a cost spike) -- see
    # _COMMODITY_TRADE_SIDE. Only CME price shocks carry clean enough
    # directional data to classify this; every disruption-style signal
    # (USACE, GDELT distress/broad sweeps) is definitionally bad news,
    # so those default to "distress" rather than leaving this unset.
    signal_type: str | None = None
    # True when this signal's commodity ALSO has an active CME price
    # shock or a classified national GDELT headline THIS sweep -- an
    # independent, different-KIND signal (macro price/news) agreeing
    # with a local/regional one (a lock disruption, a local headline),
    # not the same detector firing twice. Set after the fact by
    # _stage1_detect_signals / the Agriculture waterfall's signal
    # collection, never by an individual detector itself -- a detector
    # has no visibility into what the OTHERS found this sweep.
    macro_corroborated: bool = False


# ---------------------------------------------------------------------------
# Address matching -- shared by both waterfalls' Regrid identification step
# ---------------------------------------------------------------------------

# Field names aren't confirmed against live docs for either provider (see
# telemetry/import_genius.py, telemetry/cobalt_intelligence.py) -- this
# defensive multi-key lookup checks every plausible variant rather than
# guessing one and silently missing real matches.
_ADDRESS_KEYS = ("address", "business_address", "consignee_address", "mailing_address", "principal_address", "site_address")

# Standard bill-of-lading fields -- a customs manifest always names the
# US port the shipment actually cleared through. Field name isn't
# confirmed against Import Genius's live schema (same posture as
# _ADDRESS_KEYS), hence the multi-key defensive lookup.
_PORT_OF_ENTRY_KEYS = ("port_of_unlading", "port_of_discharge", "us_port", "arrival_port", "port_name", "discharge_port")


def _extract_port_of_entry(payload: dict) -> str | None:
    for key in _PORT_OF_ENTRY_KEYS:
        value = payload.get(key)
        if value:
            return str(value).lower()
    return None


# Maps each port_congestion MONITORED_LOCKS entry (see telemetry/usace.py)
# to the plain-language keywords its own name is built from -- matched
# against a shipment's port-of-entry text to find businesses a
# disruption actually affects by transaction, not by proximity. This is
# the DISTANCE-FREE counterpart to _geo_match_note below: a Dallas-based
# importer whose own manifest clears through a congested Houston port is
# a real, confirmed link regardless of how far Dallas is from the dock
# -- exactly the case a fixed search radius can never catch.
_PORT_TRADE_LINK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Port of Los Angeles / Long Beach anchorage": ("los angeles", "long beach", "lax", "lgb"),
    "Houston Ship Channel anchorage": ("houston",),
    "Port of New York / New Jersey anchorage": ("new york", "new jersey", "newark", "elizabeth"),
    "Port Everglades / Miami anchorage": ("miami", "port everglades", "fort lauderdale", "ft lauderdale"),
}


def _trade_link_keywords_for_target(target: "GeoTarget") -> tuple[str, ...]:
    for lock_name, keywords in _PORT_TRADE_LINK_KEYWORDS.items():
        if lock_name in target.label:
            return keywords
    return ()


def _trade_linked_port_note(payload: dict, targets: list["GeoTarget"]) -> str:
    """A business whose OWN shipment manifest names a currently-congested
    port as its port of entry is linked to that disruption by an actual
    transaction -- deliberately NOT distance-limited, unlike
    _geo_match_note. Many importers are headquartered far inland from
    the port they actually ship through; this is how those get caught
    instead of only businesses sitting physically next to the dock.
    Scoped to port_congestion targets only -- there's no equivalent
    transactional dataset for domestic river/lake barge movements (Import
    Genius is ocean customs data), so a steel_coal or grain disruption
    can't be trade-linked this way yet, only geo-matched."""
    port_of_entry = _extract_port_of_entry(payload)
    if not port_of_entry:
        return ""
    for target in targets:
        if target.commodity != "port_congestion":
            continue
        keywords = _trade_link_keywords_for_target(target)
        if keywords and any(k in port_of_entry for k in keywords):
            return f"TRADE-LINKED MATCH: shipment's port of entry '{port_of_entry}' is the actively congested port ({target.label}) -- "
    return ""


# What a shipment's own GOODS actually are, not what kind of facility
# is nearby -- distinct from _COMMODITY_LAND_USE_KEYWORDS (which
# matches a parcel's zoning/land-use text). "Argentina floods the US
# with cheap beef" is a question about WHO is actually importing beef,
# which means matching cargo content, not a company's industry tag.
_COMMODITY_GOODS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "grain": ("corn", "soybean", "wheat", "grain", "oilseed"),
    "steel_coal": ("steel", "coal", "iron ore", "coke"),
    "petrochemical": ("petrochemical", "resin", "plastic", "polymer", "chemical"),
    "food_cost": ("beef", "meat", "poultry", "dairy", "cheese", "pork"),
    "livestock_producer": ("beef", "cattle", "livestock", "meat", "pork", "dairy"),
}

# Standard bill-of-lading goods-description fields -- same defensive
# multi-key posture as _extract_address/_extract_port_of_entry; field
# name isn't confirmed against Import Genius's live schema.
_GOODS_DESCRIPTION_KEYS = ("commodity_description", "goods_description", "cargo_description", "product_description", "description")


def _extract_goods_description(payload: dict) -> str | None:
    for key in _GOODS_DESCRIPTION_KEYS:
        value = payload.get(key)
        if value:
            return str(value).lower()
    return None


def _commodity_trade_linked_note(payload: dict, targets: list["GeoTarget"]) -> str:
    """Answers "who is actually moving this commodity" directly, the
    same transactional logic as _trade_linked_port_note but keyed on
    the shipment's own GOODS content instead of its port of entry --
    works for any commodity with an active signal, not just
    port_congestion. A supermarket chain or beef wholesaler whose own
    manifest is literally FOR Argentine beef is linked to an active
    food_cost/livestock_producer signal by that shipment itself,
    regardless of which port it cleared through or how far its
    warehouse is from any lock -- this is the actual answer to
    "someone is importing that beef, who is it."

    Distance-free, same reasoning as the port version: many importers
    are headquartered far from both the disruption AND the port. Field
    name isn't confirmed against Import Genius's live schema -- see
    _extract_goods_description.
    """
    description = _extract_goods_description(payload)
    if not description:
        return ""
    checked: set[str] = set()
    for target in targets:
        if target.commodity in checked:
            continue
        checked.add(target.commodity)
        keywords = _COMMODITY_GOODS_KEYWORDS.get(target.commodity, ())
        if keywords and any(k in description for k in keywords):
            weight_note = "" if target.weight >= 0.5 else f", secondary signal only ({target.weight:.0%})"
            return (
                f"COMMODITY TRADE-LINKED MATCH: shipment's own goods description '{description}' matches "
                f"the active {target.commodity} signal ({target.label}{weight_note}) -- this is who's actually "
                f"moving that commodity, not just a business near a disruption -- "
            )
    return ""


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
# These three river-corridor radii are deliberately NOT identical,
# reflecting two different real things a "distance from the lock" is
# standing in for -- how tightly the relevant FACILITIES actually
# cluster near water, and how far a LOCK CLOSURE's disruption really
# reaches along the river before the next lock absorbs it:
#   - Petrochemical plants cluster extremely tightly around specific
#     industrial corridors (Houston Ship Channel, the Baton Rouge
#     chemical corridor) -- a wide net here just pulls in unrelated
#     land uses, so this is the TIGHTEST radius.
#   - Grain elevators are sparse and spread along long inter-lock
#     stretches (Upper Mississippi/Illinois Waterway locks are often
#     20-40 river miles apart) -- too tight a radius here misses real
#     elevators that are still genuinely served by the disrupted
#     segment, so this stays the WIDEST of the three.
#   - Ohio River steel/coal facilities sit in between: real clustering
#     around specific metro corridors (Pittsburgh, Steubenville/
#     Weirton), but tighter inter-lock spacing than the Mississippi, so
#     a middle-ground radius avoids pulling in the NEXT lock's catchment.
_COMMODITY_SEARCH_RADIUS_MILES: dict[str, float] = {
    "grain": 25.0,
    "steel_coal": 15.0,
    "petrochemical": 10.0,
    "port_congestion": 60.0,
    # Trucking companies aren't tied to one dock the way a mill or
    # elevator is -- a diesel-price shock affects fleets regionally, so
    # this needs the same wide radius as port congestion, not a tight
    # waterside-facility one.
    "logistics_fuel": 60.0,
    # Restaurants and food processors are everywhere, not clustered at
    # a dock or plant -- same wide-catchment logic as port_congestion/
    # logistics_fuel. Mostly relevant via the nationwide sweep (a
    # cattle/hog/dairy price shock has no single location), not a
    # specific lock radius.
    "food_cost": 50.0,
    # Ranches, feedlots, and dairy farms are genuinely rural and
    # dispersed -- even wider than food_cost's catchment, and mostly
    # relevant via the nationwide sweep for the same reason (a price
    # shock has no single location).
    "livestock_producer": 60.0,
    # Government-contract-driven industries (aerospace/defense) have no
    # single lock the way a river commodity does, and no CME price
    # proxy to tag a specific place with -- these flow through the
    # nationwide sweep almost exclusively, same wide-catchment logic.
    # Honest limitation: the nationwide sweep's 6 cities are chosen for
    # YOUR book's real state concentration, not for where aerospace
    # actually clusters in the real world (Wichita, Seattle, Fort
    # Worth) -- CA and TX overlap, Wichita/Seattle do not. A dedicated
    # aerospace-hub sweep list would need to be a separate addition,
    # not reuse this one, if that geographic mismatch matters enough.
    "aerospace_defense": 50.0,
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
    "logistics_fuel": ("trucking", "logistics", "freight", "warehouse", "distribution", "transportation", "fleet"),
    "food_cost": ("restaurant", "food service", "meat processing", "dairy processing", "food distribution", "catering"),
    "livestock_producer": ("cattle ranch", "feedlot", "dairy farm", "livestock", "ranch", "slaughterhouse", "meat packing"),
    "aerospace_defense": ("aerospace", "defense contractor", "aircraft", "avionics", "missile", "satellite", "aviation"),
}


def _extract_land_use(properties: dict) -> str | None:
    for key in _LAND_USE_KEYS:
        value = properties.get(key)
        if value:
            return str(value).lower()
    return None


@dataclass
class GeoTarget:
    # None for a nationwide-sweep target (see _geo_targets_from_signals)
    # -- there's no real geography behind "steel prices moved
    # everywhere," so this deliberately has no coordinates rather than
    # a fake placeholder. _nearest_geo_target skips any target with no
    # coordinates entirely -- Regrid's radius-based geo-matching only
    # ever makes sense against a real disrupted point; a nationwide
    # target drives Apollo/Cobalt (via location_hint/state) instead.
    lat: float | None
    lon: float | None
    commodity: str
    label: str
    radius_mi: float
    # Share of the originating corridor's traffic this commodity actually
    # represents (see MacroSignal.weight) -- 1.0 for a dominant/
    # single-commodity corridor, lower for a secondary share of a mixed
    # one (e.g. grain's 20-35% share of an Ohio River lock). Surfaced in
    # the match note so a candidate found via a secondary commodity reads
    # as lower-confidence than one from a dominant signal, not identical.
    weight: float = 1.0
    # Real named city/state for this target, straight from the
    # originating MacroSignal -- see _stage2b_apollo_industry_hunt.
    location_hint: str | None = None
    # Plain 2-letter state code -- see _stage2c_cobalt_industry_hunt.
    state: str | None = None
    # "expansion" or "distress" -- see MacroSignal.signal_type. Surfaced
    # in the Apollo/Cobalt hunt notes so a candidate found off a price
    # windfall reads as a growth/stacking opportunity, not identical
    # framing to one found off a plant fire.
    signal_type: str | None = None
    # See MacroSignal.macro_corroborated -- surfaced in the hunt notes
    # and scored as a confidence bonus, same idea as Interzoid's
    # cross-source confirmation but one level up: here it's two
    # DIFFERENT KINDS of signal (a macro price/news read vs. a local
    # disruption) agreeing, not two providers agreeing on one company.
    macro_corroborated: bool = False


# A nationwide commodity price shock (CME Globex, GDELT's national
# sweep) has no single place -- HRC steel moving 6% doesn't happen
# "near Pittsburgh," it happens everywhere steel is priced at once.
# Without a sweep location, that signal could only ever open a gate,
# never drive a targeted hunt -- these are the real cities the hunt
# sweeps across instead: your own top 6 states by actual lead
# concentration in the real book (CA/FL/NY/TX/NJ/IL -- pulled from the
# real sheet data earlier this session), using each state's major
# metro, NOT the river/port-specific towns MONITORED_LOCKS uses (that
# list was built for river/dock proximity, not general business
# density -- Joliet/Marseilles/Peoria aren't where you'd look for a
# restaurant or a steel distributor).
_NATIONWIDE_SWEEP_LOCATIONS: list[dict] = [
    {"apollo_location": "Los Angeles, California, US", "state": "CA"},
    {"apollo_location": "Miami, Florida, US", "state": "FL"},
    {"apollo_location": "New York, New York, US", "state": "NY"},
    {"apollo_location": "Houston, Texas, US", "state": "TX"},
    {"apollo_location": "Newark, New Jersey, US", "state": "NJ"},
    {"apollo_location": "Chicago, Illinois, US", "state": "IL"},
]


def _geo_targets_from_signals(signals: list[MacroSignal]) -> list[GeoTarget]:
    """One search target per active signal that carries a commodity.

    When a signal ALSO carries a specific location (today, only
    USACE-origin and GDELT-local ones do), the target is anchored
    exactly there, same as before. When it carries a commodity but NO
    location -- a nationwide CME Globex price shock, or GDELT's
    national sweep -- this no longer just gets skipped: it sweeps
    _NATIONWIDE_SWEEP_LOCATIONS instead, producing one target per real
    metro so the hunt still runs, just across your own territory
    instead of one exact disrupted point. This is the piece that turns
    "something disrupted somewhere" (or "steel prices just moved
    everywhere") into "go look specifically here, for this kind of
    business."

    The existing per-location/per-state dedup inside the Apollo/Cobalt
    hunts already protects against call-count blowup here -- several
    signals (or a nationwide sweep's several cities) resolving to the
    same place/state still costs exactly one paid call for it, not one
    per signal.
    """
    targets = []
    for signal in signals:
        if not signal.commodity:
            continue
        radius = _COMMODITY_SEARCH_RADIUS_MILES.get(signal.commodity, _DEFAULT_SEARCH_RADIUS_MILES)

        if signal.coordinates:
            lat, lon = signal.coordinates
            targets.append(
                GeoTarget(
                    lat=lat, lon=lon, commodity=signal.commodity, label=signal.description,
                    radius_mi=radius, weight=signal.weight, location_hint=signal.location_hint,
                    state=signal.state, signal_type=signal.signal_type,
                    macro_corroborated=signal.macro_corroborated,
                )
            )
            continue

        # No single place -- sweep the nationwide locations instead of
        # skipping this signal entirely.
        for place in _NATIONWIDE_SWEEP_LOCATIONS:
            targets.append(
                GeoTarget(
                    lat=None, lon=None,  # no real geography behind a nationwide sweep -- see GeoTarget docstring
                    commodity=signal.commodity, label=f"{signal.description} (nationwide sweep: {place['apollo_location']})",
                    radius_mi=radius, weight=signal.weight,
                    location_hint=place["apollo_location"], state=place["state"], signal_type=signal.signal_type,
                    macro_corroborated=signal.macro_corroborated,
                )
            )
    return targets


def _nearest_geo_target(coordinates: tuple[float, float] | None, targets: list[GeoTarget]) -> tuple[GeoTarget, float] | None:
    """The closest target a parcel falls within the radius of, or None if
    it's outside every active target's radius (or there are no active
    geo-tagged signals this sweep, or the parcel has no coordinates).
    Targets with no coordinates of their own (nationwide-sweep targets
    -- see GeoTarget) are skipped here entirely: there's no real
    geography to measure a distance against, so they drive the Apollo/
    Cobalt hunts via location_hint/state instead, never Regrid
    geo-matching."""
    if not coordinates or not targets:
        return None
    lat, lon = coordinates
    best: tuple[GeoTarget, float] | None = None
    for target in targets:
        if target.lat is None or target.lon is None:
            continue
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
    weight_note = "" if target.weight >= 0.5 else f", secondary signal only ({target.weight:.0%} of this corridor's traffic)"
    if land_use and keywords and any(k in land_use for k in keywords):
        return (
            f"TARGETED MATCH: {distance:.1f}mi from active {target.commodity} signal ({target.label}{weight_note}), "
            f"land-use '{land_use}' confirms {target.commodity} classification -- "
        )
    if land_use:
        return (
            f"Near active {target.commodity} signal ({target.label}{weight_note}, {distance:.1f}mi) but land-use "
            f"'{land_use}' doesn't confirm {target.commodity} -- radius match only, not industry-confirmed -- "
        )
    return (
        f"Near active {target.commodity} signal ({target.label}{weight_note}, {distance:.1f}mi); land-use classification "
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


# Maps each commodity tag to the industry keywords an active-hunt Apollo
# search should actually ask for -- this is deliberately separate from
# _COMMODITY_LAND_USE_KEYWORDS above (that one matches Regrid's land-use
# TEXT; this one is what gets sent to Apollo's own org-search API, which
# has its own keyword-tag vocabulary, not a shared one with Regrid).
_COMMODITY_APOLLO_KEYWORDS: dict[str, list[str]] = {
    "grain": ["grain elevator", "agricultural exporter", "grain trading", "farm supply"],
    "steel_coal": ["steel mill", "steel fabrication", "metal service center", "coal mining", "primary metal manufacturing"],
    "petrochemical": ["petrochemical", "chemical manufacturing", "petroleum refining", "plastics manufacturing"],
    "port_congestion": ["import", "distribution center", "freight forwarding", "warehousing and storage", "wholesale trade"],
    "logistics_fuel": ["trucking company", "freight carrier", "logistics company", "transportation company", "trucking fleet"],
    "food_cost": ["restaurant", "food service company", "meat processing", "dairy processing", "food distributor", "catering company"],
    "livestock_producer": ["cattle ranch", "feedlot operator", "dairy farm", "livestock producer", "meat packing plant", "slaughterhouse"],
    "aerospace_defense": ["aerospace manufacturer", "defense contractor", "aircraft parts supplier", "avionics company", "aerospace supplier"],
}

# Who actually makes a financing decision at a small/mid-size business --
# not every title Apollo could return, just the ones worth calling.
_DECISION_MAKER_TITLES = ["Owner", "President", "CEO", "Founder", "CFO", "Controller", "VP Finance", "General Manager"]

# Apollo org objects carry their internal id under one of these keys,
# depending on API version -- same defensive multi-key posture as
# _ADDRESS_KEYS/_PORT_OF_ENTRY_KEYS above, not asserting one confirmed
# name.
_ORG_ID_KEYS = ("id", "organization_id", "organization_id_str")


def _extract_org_id(payload: dict) -> str | None:
    for key in _ORG_ID_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _stage2b_apollo_industry_hunt(db: Session, targets: list[GeoTarget]) -> list[tuple[str, str, str, tuple[float, float] | None]]:
    """The one identification path that actively HUNTS instead of only
    checking whatever ImportGenius/Regrid data already happened to flow
    in. There's no barge-manifest vendor to lean on for river/lake
    corridors (see the "can we get barge manifest data" discussion --
    no such market exists), so this uses firmographic/industry data
    instead of cargo content: when a disruption fires, actively search
    Apollo for companies in the industry it implies, near the real place
    it happened.

    Apollo's org search filters by a named location string (city/state),
    not a lat/lon radius -- see telemetry/apollo.py -- so this uses each
    target's `location_hint` (explicit per-lock data in usace.py's
    MONITORED_LOCKS, not derived from coordinates) rather than inventing
    a geo-radius parameter Apollo's real API doesn't support.

    One Apollo call per distinct location per sweep, not per target --
    several commodities sharing one lock (e.g. steel_coal + grain on the
    same Ohio River lock) must not trigger two separate paid API calls
    for the same place. A bad/failed call is swallowed per-location (one
    flaky request must not drop every other target's results), same
    posture as TelemetryAdapter.ingest.

    Every result is written as its own TelemetryEvent, tagged
    triggered_by_disruption, before being returned -- a candidate here
    must trace back to a real stored row via source_reference, same
    invariant as every other identification path in this module, not an
    in-memory API response that vanishes after this sweep.

    For every company found, this ALSO tries Apollo's People Search
    (adapter.search_people) for a named decision-maker (_DECISION_MAKER_
    TITLES) at that specific organization -- turning "here's a company"
    into "here's who to actually ask for," not just a company switchboard
    number. A people-search failure never costs the company result
    itself -- it just falls back to "no named decision-maker found yet."
    Contact fields land in the SAME TelemetryEvent under the generic
    phone/email keys _extract_contact_fields already checks, so the
    existing _stage4_contact_enrichment picks them up with no changes of
    its own needed.
    """
    api_key = get_settings().apollo_api_key
    if not api_key:
        return []

    adapter = ApolloAdapter(api_key=api_key)
    identified: list[tuple[str, str, str, tuple[float, float] | None]] = []
    seen_locations: set[str] = set()

    for target in targets:
        if not target.location_hint or target.location_hint in seen_locations:
            continue
        seen_locations.add(target.location_hint)

        keywords = _COMMODITY_APOLLO_KEYWORDS.get(target.commodity)
        if not keywords:
            continue

        try:
            records = adapter.search(keywords, [target.location_hint])
        except Exception:
            continue  # one bad call must not drop every other target's results this sweep

        weight_note = "" if target.weight >= 0.5 else f", secondary signal only ({target.weight:.0%} of this corridor's traffic)"
        for record in records:
            name = record.payload.get("name") or record.title
            contact_fields: dict = {}
            contact_note = "no named decision-maker found yet -- company-level result only"
            org_id = _extract_org_id(record.payload)
            if org_id:
                try:
                    people = adapter.search_people(org_id, _DECISION_MAKER_TITLES)
                except Exception:
                    people = []  # a people-search failure must not cost us the company result itself
                if people:
                    contact_fields = people[0].payload
                    contact_note = f"decision-maker found: {contact_fields.get('contact_name')} ({contact_fields.get('contact_title')})"

            event = TelemetryEvent(
                source=TelemetrySource.APOLLO,
                title=record.title,
                payload={
                    **record.payload,
                    **contact_fields,  # contact_name/contact_title/phone/email, when found -- see search_people
                    "triggered_by_disruption": target.label,
                    "hunt_commodity": target.commodity,
                    "hunt_location": target.location_hint,
                },
            )
            db.add(event)
            db.flush()  # need event.id to build a real source_reference below
            situation = (
                "EXPANSION opportunity -- this commodity move is a revenue/margin windfall for this industry, "
                "a growth/stacking angle, not a rescue pitch"
                if target.signal_type == "expansion"
                else "DISTRESS opportunity -- this commodity move is a cost/margin squeeze for this industry"
                if target.signal_type == "distress"
                else "unclassified commodity move"
            )
            corroboration_note = (
                " | MACRO-CORROBORATED: a CME price shock or national GDELT headline independently "
                "confirms this commodity is active right now, not just one local read"
                if target.macro_corroborated else ""
            )
            note = (
                f"APOLLO INDUSTRY HUNT: actively searched for {keywords} companies near {target.location_hint} "
                f"because of an active {target.commodity} signal ({target.label}{weight_note}) -- {situation} -- "
                f"this company was not already sitting in any other feed, found by live industry+location search, "
                f"not cargo data | {contact_note}{corroboration_note}"
            )
            identified.append((str(name), f"telemetry_event:{event.id} [apollo] {record.title}", note, None))

    db.commit()
    return identified


# One representative search term per commodity -- Cobalt's SOS search
# takes a single term, not a keyword list the way Apollo's does.
_COMMODITY_COBALT_SEARCH_TERMS: dict[str, str] = {
    "grain": "grain elevator",
    "steel_coal": "steel",
    "petrochemical": "petrochemical",
    "port_congestion": "distribution",
    "logistics_fuel": "trucking",
    "food_cost": "restaurant",
    "livestock_producer": "livestock",
    "aerospace_defense": "aerospace",
}


def _stage2c_cobalt_industry_hunt(db: Session, targets: list[GeoTarget]) -> list[tuple[str, str, str, tuple[float, float] | None]]:
    """The Cobalt Intelligence sibling of _stage2b_apollo_industry_hunt --
    same idea (actively search by industry near an active disruption,
    instead of only checking whatever already flowed in), different API
    shape: Cobalt's SOS search takes a STATE, not a city, and ONE search
    term, not a keyword list (see telemetry/cobalt_intelligence.py). One
    call per distinct state per sweep, not per target, same reasoning as
    Apollo's per-location dedup -- several commodities/targets sharing
    one lock's state must not trigger duplicate paid calls.

    A genuine extra on top of discovery: every result IS a live SOS
    standing check, so this reads the same distress/good-standing
    keywords _stage3_lending_appetite uses and surfaces that read
    directly in the note -- a Cobalt-sourced candidate already carries a
    standing signal before stage 3 even runs (stage 3 still re-checks it
    by name independently; that's redundant, not wrong, since neither
    check is allowed to assume what the other already found).

    Also requests UCC filing data on the same call (see
    telemetry/cobalt_intelligence.py's uccData=true) -- when a state
    Cobalt covers for UCC returns one, it's surfaced in the note too,
    answering the much earlier "can we get UCC data" question for
    whichever of the real 11 covered states this happens to land in,
    through a vendor already configured here rather than a new one.

    Same fault-isolation and real-stored-row invariants as the Apollo
    hunt: one bad call doesn't drop other states' results, and every
    candidate traces back to a real TelemetryEvent via source_reference.
    """
    api_key = get_settings().cobalt_intelligence_api_key
    if not api_key:
        return []

    adapter = CobaltIntelligenceAdapter(api_key=api_key)
    identified: list[tuple[str, str, str, tuple[float, float] | None]] = []
    seen_states: set[str] = set()

    for target in targets:
        if not target.state or target.state in seen_states:
            continue
        seen_states.add(target.state)

        search_term = _COMMODITY_COBALT_SEARCH_TERMS.get(target.commodity)
        if not search_term:
            continue

        try:
            records = adapter.search(target.state, search_term)
        except Exception:
            continue  # one bad call must not drop every other state's results this sweep

        weight_note = "" if target.weight >= 0.5 else f", secondary signal only ({target.weight:.0%} of this corridor's traffic)"
        for record in records:
            name = record.payload.get("name") or record.title
            status = str(record.payload.get("status") or record.payload.get("standing") or "").lower()
            if any(k in status for k in _DISTRESS_STATUS_KEYWORDS):
                standing_note = f"standing: '{status}' -- NOT in good standing, verify before pursuing"
            elif any(k in status for k in _GOOD_STANDING_KEYWORDS):
                standing_note = f"standing: '{status}' -- confirmed good standing"
            else:
                standing_note = "standing: unconfirmed from this record"
            ucc_note = ""
            if record.payload.get("uccFilings") or record.payload.get("ucc_filings") or record.payload.get("uccData"):
                ucc_note = " | UCC filing data present on this record"

            event = TelemetryEvent(
                source=TelemetrySource.COBALT_INTELLIGENCE,
                title=record.title,
                payload={
                    **record.payload,
                    "triggered_by_disruption": target.label,
                    "hunt_commodity": target.commodity,
                    "hunt_state": target.state,
                },
            )
            db.add(event)
            db.flush()
            situation = (
                "EXPANSION opportunity (revenue/margin windfall, growth angle)"
                if target.signal_type == "expansion"
                else "DISTRESS opportunity (cost/margin squeeze)"
                if target.signal_type == "distress"
                else "unclassified commodity move"
            )
            corroboration_note = (
                " | MACRO-CORROBORATED: a CME price shock or national GDELT headline independently "
                "confirms this commodity is active right now, not just one local read"
                if target.macro_corroborated else ""
            )
            note = (
                f"COBALT INTELLIGENCE INDUSTRY HUNT: actively searched SOS registry for '{search_term}' businesses "
                f"in {target.state} because of an active {target.commodity} signal ({target.label}{weight_note}) "
                f"-- {situation} -- this company was not already sitting in any other feed | "
                f"{standing_note}{ucc_note}{corroboration_note}"
            )
            identified.append((str(name), f"telemetry_event:{event.id} [cobalt_intelligence] {record.title}", note, None))

    db.commit()
    return identified


# ---------------------------------------------------------------------------
# Shared signal detectors
# ---------------------------------------------------------------------------

# A same-day |close-open| move of this size on any tracked CBOT
# grain/oilseed contract counts as an active price-shock signal.
PRICE_SHOCK_PCT = 0.02

_DISTRESS_STATUS_KEYWORDS = ("suspended", "dissolved", "revoked", "inactive", "delinquent", "forfeited")
_GOOD_STANDING_KEYWORDS = ("active", "good standing", "in good standing", "current")


# Maps every CME Globex symbol (see telemetry/cme_globex.py::SYMBOLS)
# to the same commodity vocabulary USACE/Apollo/Cobalt already use. This
# MUST stay in sync with that SYMBOLS list -- an untagged symbol is
# silently treated as having no commodity (coordinates/location_hint
# never get set for it, so it can still open a signal gate but can never
# drive a targeted hunt or be commodity-filtered), not an error, but it
# also means it would never wrongly contaminate a commodity-filtered
# waterfall like Agriculture's.
# A single price move can genuinely hit two different populations with
# OPPOSITE financing stories at once -- a cattle/hog/dairy price crash
# (e.g. a trade-policy move letting in a flood of cheap imports) is
# DISTRESS for the ranchers/feedlots/dairy farms who sell it, and mild
# RELIEF for the restaurants/food processors who buy it as an input.
# One commodity tag per symbol can't represent that -- each livestock/
# dairy symbol carries BOTH tags, each independently classified by its
# own trade side (see _COMMODITY_TRADE_SIDE), so "Argentina beef floods
# the market" correctly produces a distress signal for cattle producers
# AND a separate, differently-framed signal for food-cost-side
# consumers from the exact same price move, not one signal pretending
# to speak for both sides.
SYMBOL_COMMODITIES: dict[str, list[str]] = {
    "ZC.c.0": ["grain"], "ZS.c.0": ["grain"], "ZW.c.0": ["grain"], "ZM.c.0": ["grain"], "ZL.c.0": ["grain"],
    "HRC.c.0": ["steel_coal"],
    "CL.c.0": ["logistics_fuel"], "HO.c.0": ["logistics_fuel"],
    "NG.c.0": ["petrochemical"],
    "LE.c.0": ["livestock_producer", "food_cost"],
    "GF.c.0": ["livestock_producer", "food_cost"],
    "HE.c.0": ["livestock_producer", "food_cost"],
    "DC.c.0": ["livestock_producer", "food_cost"],
}

# Which side of the trade the businesses we actually target under each
# commodity sit on -- a price move only tells you "borrow" or "margin
# squeeze" once you know whether that business SELLS this commodity or
# BUYS it as an input. A price shock has never been treated as
# direction-neutral info in real commodity markets; a producer getting
# a windfall on a price spike is the OPPOSITE financing story from a
# producer getting squeezed by a price crash, and it's the opposite
# again for a business that buys this commodity as an input instead of
# selling it.
#   "producer": grain elevators/exporters and steel mills are sellers
#     of what we're tracking -- a price UP is a revenue windfall
#     (expansion-financing moment, the thing being targeted is
#     creditworthy and growing), a price DOWN is a margin squeeze
#     (distress-financing moment).
#   "consumer": petrochemical plants (NG/crude feedstock), trucking
#     fleets (diesel), and restaurants/food processors (cattle/hog/
#     dairy) all BUY this commodity as a cost input -- for them it's
#     the exact mirror image: a price UP squeezes margin (distress), a
#     price DOWN is relief, not itself a strong borrowing trigger.
_COMMODITY_TRADE_SIDE: dict[str, str] = {
    "grain": "producer",
    "steel_coal": "producer",
    "petrochemical": "consumer",
    "logistics_fuel": "consumer",
    "food_cost": "consumer",
    # The seller side of the livestock/dairy complex -- ranches,
    # feedlots, dairy farms, slaughterhouses/meat packers. A price DOWN
    # (e.g. cheap imports flooding the market) squeezes THEM even
    # though it's relief for food_cost's restaurant/processor side.
    "livestock_producer": "producer",
}


def _cme_price_shock_signals(db: Session, commodities: set[str] | None = None) -> list[MacroSignal]:
    """A same-day price shock on ANY tracked CME/NYMEX symbol -- not just
    the original grain complex (see telemetry/cme_globex.py::SYMBOLS,
    which now also covers steel, crude/diesel, natural gas, and the
    livestock/dairy complex). Every shock is tagged with its commodity
    via SYMBOL_COMMODITIES so a caller can filter the same way
    _usace_disruption_signals already requires: the Agriculture & Grain
    Handling waterfall passes {"grain"} so a steel or diesel price
    shock can't gate it open by mistake, while the CME Macro Funnel
    passes None for every commodity. A symbol can carry MULTIPLE
    commodity tags -- a livestock/dairy price move genuinely creates
    two different populations with OPPOSITE financing stories
    (ranchers who sell it vs. restaurants who buy it as an input), so
    one signal is emitted PER tag, each independently classified, not
    one signal pretending to speak for both sides.

    ALSO classifies each shock as "expansion" or "distress" via
    _COMMODITY_TRADE_SIDE x direction -- a price move isn't
    direction-neutral "look here" information; a producer riding a
    windfall is a completely different financing story than a producer
    getting squeezed, and the reverse is true for a business that buys
    this commodity as a cost input rather than selling it. This is what
    actually answers "when things are good, people borrow too" instead
    of treating every big move as implicitly bad news.

    A nationwide price move has no single lat/lon the way a specific
    lock does, so coordinates/location_hint/state are left unset here
    -- _geo_targets_from_signals sweeps your own top-state metros
    instead when it sees a commodity with no location attached, so
    this still drives a real targeted hunt downstream.
    """
    signals: list[MacroSignal] = []
    for event in _fresh_events(db, TelemetrySource.CME_GLOBEX, limit=50):
        open_, close = event.payload.get("open"), event.payload.get("close")
        if not open_ or not close:
            continue
        pct = (close - open_) / open_
        if abs(pct) < PRICE_SHOCK_PCT:
            continue
        symbol = event.payload.get("symbol") or str(event.payload.get("instrument_id", "?"))
        price_up = pct > 0
        direction = "up" if price_up else "down"

        for commodity in SYMBOL_COMMODITIES.get(symbol, []):
            if commodities is not None and commodity not in commodities:
                continue

            trade_side = _COMMODITY_TRADE_SIDE.get(commodity)
            if trade_side == "producer":
                signal_type = "expansion" if price_up else "distress"
            elif trade_side == "consumer":
                signal_type = "distress" if price_up else "expansion"
            else:
                signal_type = None  # untagged/unclassified commodity -- don't guess

            framing = (
                f"{signal_type} signal ({trade_side} of this commodity)" if signal_type else "shock (unclassified)"
            )
            signals.append(
                MacroSignal(
                    kind="price_shock",
                    description=(
                        f"CME Globex {symbol} moved {abs(pct):.1%} {direction} intraday "
                        f"(open {open_:.2f} -> close {close:.2f}) -- {commodity}: {framing}"
                    ),
                    source_reference=f"telemetry_event:{event.id} [cme_globex] {event.title}",
                    signal_type=signal_type,
                    commodity=commodity,
                )
            )
    return signals


def _classify_commodity_from_text(text: str) -> str | None:
    """Best-guess commodity classification from keyword text alone, no
    place required -- lets a NATIONAL headline (no location to anchor
    to at all) still contribute to "which commodities are hot right
    now" (see _hot_commodities_this_sweep), the same crude keyword
    match _gdelt_broad_disruption_signals already uses, just without
    requiring a place match alongside it. Not a verified tag -- a
    headline can legitimately mention more than one industry; this
    returns whichever matches first, good enough for a priority signal,
    not for identifying a specific business."""
    text_lower = text.lower()
    for commodity, keywords in _COMMODITY_LAND_USE_KEYWORDS.items():
        if any(k in text_lower for k in keywords):
            return commodity
    return None


def _highergov_contract_signals(db: Session, commodities: set[str] | None = None) -> list[MacroSignal]:
    """A government contract AWARD is a real, confirmed demand event --
    not just for the awardee, but one step removed, for every supplier
    in that industry's supply chain (an aerospace prime winning a
    contract means its subcontractor network needs to scale too).
    Classified by the same keyword-text technique as GDELT's national
    sweep (_classify_commodity_from_text), since most government-
    contract-driven industries (aerospace/defense being the clearest
    example) have no CME-listed futures contract the way steel or
    grain does -- there's no price proxy to tag a place with.

    Always "expansion" -- winning a contract is a growth event, never
    read as distress the way a price crash or plant fire is.

    HigherGov's place-of-performance field name isn't confirmed against
    its live API (see telemetry/highergov.py's own TODO), and even if
    it were, its format (state abbreviation? full city? agency region?)
    isn't something to guess at and silently mis-feed into Apollo's
    location filter. Coordinates/location_hint/state are deliberately
    left unset -- this flows through the nationwide sweep instead, same
    as a CME price shock with no single place."""
    signals: list[MacroSignal] = []
    for event in _fresh_events(db, TelemetrySource.HIGHERGOV, limit=20):
        text = f"{event.title} {event.payload.get('description', '')}"
        commodity = _classify_commodity_from_text(text)
        if not commodity:
            continue
        if commodities is not None and commodity not in commodities:
            continue
        signals.append(
            MacroSignal(
                kind="contract_award",
                description=f"HigherGov contract award classified {commodity}: {event.title}",
                source_reference=f"telemetry_event:{event.id} [highergov] {event.title}",
                commodity=commodity,
                signal_type="expansion",
            )
        )
    return signals


def _gdelt_shock_signals(db: Session) -> list[MacroSignal]:
    # GDELT's query (see telemetry/gdelt.py::COMMODITY_SHOCK_QUERY) is
    # already scoped to shock-relevant language -- "commodities crash",
    # "grain shortage", "export ban", theme:ECON_COMMODITY -- AND
    # tariff/trade-policy language ("tariff", "trade war", Section
    # 301/232) -- so any fresh match IS the signal; no extra magnitude
    # filter needed on top of it. Deliberately leaves signal_type unset
    # (not forced to "distress") -- this query mixes genuinely
    # direction-ambiguous news (a new tariff can be good news for a
    # protected domestic producer, bad for an importer), so guessing a
    # direction here would be worse than leaving it unclassified.
    return [
        MacroSignal(
            kind="news_shock",
            description=f"GDELT commodity/tariff-shock coverage: {event.title}",
            source_reference=f"telemetry_event:{event.id} [gdelt] {event.title}",
            commodity=_classify_commodity_from_text(event.title),
        )
        for event in _fresh_events(db, TelemetrySource.GDELT, limit=10)
    ]


# Distress/operational-disruption language, per commodity -- combined
# with a required local place name below to build a genuinely LOCAL
# query, as opposed to COMMODITY_SHOCK_QUERY's national one. A strike,
# fire, or shutdown at one specific plant would never make the national
# feed, but it's exactly the kind of event that creates an urgent, local
# capital need -- this is what actually catches it.
_COMMODITY_DISTRESS_LANGUAGE: dict[str, str] = {
    "grain": '("grain elevator" OR "crop failure" OR drought OR flood OR "grain shortage" OR "export ban")',
    "steel_coal": '("steel mill" OR "steel plant" OR "coal mine" OR layoffs OR strike OR shutdown OR explosion OR fire)',
    "petrochemical": '("petrochemical plant" OR refinery OR "chemical plant" OR explosion OR fire OR leak OR shutdown)',
    "port_congestion": '(port OR terminal OR "shipping delay" OR congestion OR backlog OR strike)',
    "logistics_fuel": '(trucking OR "freight carrier" OR "fuel surcharge" OR "diesel price" OR layoffs OR bankruptcy)',
}


def _gdelt_local_distress_signals(db: Session, commodities: set[str] | None = None) -> list[MacroSignal]:
    """The LOCAL counterpart to _gdelt_shock_signals' national sweep.
    Actively queries GDELT once per distinct monitored place (reusing
    usace.py's MONITORED_LOCKS rather than maintaining a second location
    list) for distress language specific to that place's commodity mix.
    GDELT is free/keyless, so unlike the Apollo/Cobalt hunts there's no
    per-call cost to manage -- dedup here is purely to avoid firing the
    same identical query twice when two lock entries share a city (e.g.
    the two Pittsburgh locks), not a budget concern.

    A caller filters by commodity exactly like _usace_disruption_signals
    and _cme_price_shock_signals -- the Agriculture & Grain Handling
    waterfall passes {"grain"}, the CME Macro Funnel passes None.

    Every hit is written as its own TelemetryEvent (tagged
    local_news=True) before becoming a MacroSignal, same
    real-stored-row invariant as every active-hunt path in this module.
    A bad/failed call is swallowed per-location, same fault-isolation
    posture as the Apollo/Cobalt hunts.
    """
    adapter = GDELTAdapter()
    signals: list[MacroSignal] = []
    seen_locations: set[str] = set()

    for lock in MONITORED_LOCKS:
        location = lock.get("apollo_location")
        if not location or location in seen_locations:
            continue
        seen_locations.add(location)

        place = location.split(",")[0].strip()  # "Pittsburgh, Pennsylvania, US" -> "Pittsburgh"
        lat, lon, state = lock.get("lat"), lock.get("lon"), lock.get("state")

        for commodity, weight in (lock.get("commodities") or {}).items():
            if weight < _MIN_COMMODITY_WEIGHT:
                continue
            if commodities is not None and commodity not in commodities:
                continue
            distress_terms = _COMMODITY_DISTRESS_LANGUAGE.get(commodity)
            if not distress_terms:
                continue

            try:
                records = adapter.search(f'{distress_terms} AND "{place}"')
            except Exception:
                continue  # one bad call must not drop every other location's results this sweep

            for record in records:
                event = TelemetryEvent(
                    source=TelemetrySource.GDELT,
                    title=record.title,
                    payload={**record.payload, "local_news": True, "commodity": commodity, "lat": lat, "lon": lon, "state": state, "place": place},
                )
                db.add(event)
                db.flush()
                label = commodity.replace("_", " ")
                signals.append(
                    MacroSignal(
                        kind="local_news_distress",
                        description=f"GDELT local {label} distress coverage near {place} ({weight:.0%} of this corridor's traffic): {record.title}",
                        source_reference=f"telemetry_event:{event.id} [gdelt] {record.title}",
                        commodity=commodity,
                        coordinates=(float(lat), float(lon)) if lat is not None and lon is not None else None,
                        weight=weight,
                        location_hint=location,
                        state=state,
                        signal_type="distress",
                    )
                )

    db.commit()
    return signals


# Generic business-distress language, with NO place or commodity baked
# in -- the point of this query is to cast a wider net than
# _gdelt_local_distress_signals' precise per-place-per-commodity
# queries, catching disruption language that specific keyword list
# didn't anticipate (a plant "idles," "cuts shifts," "furloughs
# workers" instead of the exact words already in
# _COMMODITY_DISTRESS_LANGUAGE).
_BROAD_DISRUPTION_QUERY = (
    '(fire OR explosion OR layoffs OR strike OR shutdown OR bankruptcy OR "plant closure" '
    'OR closure OR flood OR idled OR furlough OR "cuts shifts" OR recall)'
)


def _build_place_commodity_index() -> dict[str, dict]:
    """city name (lowercase) -> {lat, lon, state, commodities: set of
    commodities plausible at that specific place}. Built from
    MONITORED_LOCKS (real lat/lon + that lock's own real commodity mix)
    first, then _NATIONWIDE_SWEEP_LOCATIONS fills in any of your top-6
    metros not already covered by a lock (no real lat/lon for those --
    a metro isn't anchored to one specific industry the way a lock is,
    so every OTHER commodity is left plausible there; food_cost and
    livestock_producer are deliberately excluded from place-based
    classification -- restaurants aren't geographically concentrated
    the way a mill or elevator is, and ranches/feedlots are the
    opposite, genuinely rural and specifically NOT in a major metro --
    both are better served by the nationwide sweep already built, not
    a "mentioned near this city" guess)."""
    index: dict[str, dict] = {}
    for lock in MONITORED_LOCKS:
        city = lock["apollo_location"].split(",")[0].strip().lower()
        if city not in index:
            index[city] = {"lat": lock["lat"], "lon": lock["lon"], "state": lock["state"], "commodities": set((lock.get("commodities") or {}).keys())}
    metro_plausible_commodities = set(_COMMODITY_LAND_USE_KEYWORDS.keys()) - {"food_cost", "livestock_producer"}
    for place in _NATIONWIDE_SWEEP_LOCATIONS:
        city = place["apollo_location"].split(",")[0].strip().lower()
        if city not in index:
            index[city] = {"lat": None, "lon": None, "state": place["state"], "commodities": metro_plausible_commodities}
    return index


def _gdelt_broad_disruption_signals(db: Session, commodities: set[str] | None = None) -> list[MacroSignal]:
    """The open-net counterpart to _gdelt_local_distress_signals' precise
    per-place queries: ONE unscoped national search for generic
    business-distress language (no place, no commodity baked into the
    query), then classify each hit against the real places and
    industries this system already knows about (see
    _build_place_commodity_index) by checking whether a known city name
    AND a matching commodity's land-use keywords both appear in the
    article's own title.

    This is honestly a cruder classification than the targeted sweep --
    a keyword match on a short article title, not a verified tag -- so
    every signal this produces says so plainly in its description. It
    is NOT full open-ended discovery of an industry/place this system
    has never heard of: GDELT's article-search API doesn't hand back
    structured "this article is about Company X in City Y" the way a
    real entity-extraction product would, so this only ever finds a
    disruption at a place/industry combination already defined here,
    just without having to have pre-combined that exact place and that
    exact commodity into a query string ahead of time. Real open-ended
    discovery would need GDELT's separate Knowledge Graph product or an
    actual text-reading step per article -- a different, bigger build,
    not this one.

    One single free GDELT call for the whole sweep, not one per place --
    cheaper and catches broader disruption language than the targeted
    sweep's AND-combined queries do.
    """
    adapter = GDELTAdapter()
    place_index = _build_place_commodity_index()

    try:
        records = adapter.search(_BROAD_DISRUPTION_QUERY, maxrecords=50)
    except Exception:
        return []  # a single failed call here just means no broad-sweep signals this sweep, not an error

    signals: list[MacroSignal] = []
    for record in records:
        title_lower = record.title.lower()
        for city, info in place_index.items():
            if city not in title_lower:
                continue
            for commodity in info["commodities"]:
                if commodities is not None and commodity not in commodities:
                    continue
                keywords = _COMMODITY_LAND_USE_KEYWORDS.get(commodity, ())
                if not any(k in title_lower for k in keywords):
                    continue

                event = TelemetryEvent(
                    source=TelemetrySource.GDELT,
                    title=record.title,
                    payload={
                        **record.payload, "local_news": True, "broad_sweep": True,
                        "commodity": commodity, "lat": info["lat"], "lon": info["lon"], "state": info["state"], "place": city,
                    },
                )
                db.add(event)
                db.flush()
                label = commodity.replace("_", " ")
                signals.append(
                    MacroSignal(
                        kind="broad_news_distress",
                        description=(
                            f"GDELT broad-sweep match: '{record.title}' mentions {city.title()} and "
                            f"{label}-related language -- approximate keyword classification, not a verified tag"
                        ),
                        source_reference=f"telemetry_event:{event.id} [gdelt] {record.title}",
                        commodity=commodity,
                        coordinates=(float(info["lat"]), float(info["lon"])) if info["lat"] is not None else None,
                        weight=1.0,
                        location_hint=next((l["apollo_location"] for l in MONITORED_LOCKS if l["apollo_location"].lower().startswith(city)), None)
                        or next((p["apollo_location"] for p in _NATIONWIDE_SWEEP_LOCATIONS if p["apollo_location"].lower().startswith(city)), None),
                        state=info["state"],
                        signal_type="distress",
                    )
                )

    db.commit()
    return signals


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


# Below this share of a corridor's traffic, a commodity isn't worth
# emitting its own signal for -- a trace amount shouldn't gate open a
# whole silo. Tunable policy, same posture as every other threshold
# constant in this module.
_MIN_COMMODITY_WEIGHT = 0.15


def _usace_disruption_signals(db: Session, commodities: set[str] | None = None) -> list[MacroSignal]:
    """Every USACE event (see telemetry/usace.py) already crossed a real
    disruption threshold before it was recorded (GATE_CHANGE_THRESHOLD /
    QUEUE_THRESHOLD) -- any fresh event IS a disruption signal, not a raw
    data point needing re-filtering. But a single disrupted lock/port can
    represent SEVERAL commodities at once (see usace.py's MONITORED_LOCKS
    `commodities` mix) -- this emits one weighted MacroSignal PER
    commodity above _MIN_COMMODITY_WEIGHT, not one signal per lock, so a
    disruption on a mixed corridor (e.g. the Ohio River's steel/coal +
    grain mix) legitimately feeds every silo it actually matters to, in
    proportion to how much of that corridor's real traffic each one
    represents, instead of forcing a single winner-take-all tag.

    A caller MUST say which commodity tag(s) it actually wants: the
    Agriculture & Grain Handling waterfall passes {"grain"} so a
    steel-corridor or port-congestion event can't gate it open by
    mistake, while the CME Macro Funnel (broader, cross-industry) passes
    None for every corridor. Each event's commodity mix was written into
    its payload by usace.py's fetch() -- read back here rather than
    re-deriving it from the lock name."""
    signals = []
    for event in _fresh_events(db, TelemetrySource.USACE, limit=20):
        commodity_mix = event.payload.get("commodities") or {}
        lat, lon = event.payload.get("lat"), event.payload.get("lon")
        coordinates = (float(lat), float(lon)) if lat is not None and lon is not None else None
        location_hint = event.payload.get("apollo_location")
        state = event.payload.get("state")

        for commodity, weight in commodity_mix.items():
            if weight < _MIN_COMMODITY_WEIGHT:
                continue  # trace share of this corridor's traffic -- not worth its own signal
            if commodities is not None and commodity not in commodities:
                continue
            label = commodity.replace("_", " ")
            signals.append(
                MacroSignal(
                    kind="river_corridor_disruption",
                    description=f"USACE {label} disruption ({weight:.0%} of this corridor's traffic): {event.title}",
                    source_reference=f"telemetry_event:{event.id} [usace] {event.title}",
                    commodity=commodity,
                    coordinates=coordinates,
                    weight=weight,
                    location_hint=location_hint,
                    state=state,
                    signal_type="distress",
                )
            )
    return signals


# ---------------------------------------------------------------------------
# CME Macro Funnel -- 4-stage sequential waterfall
# ---------------------------------------------------------------------------

CME_MACRO_SILO = SiloName.CME_MACRO_FUNNEL


def _hot_commodities_this_sweep(macro_level_signals: list[MacroSignal]) -> set[str]:
    """Commodities with an active MACRO-level read this sweep -- a real
    CME price shock, or a national GDELT headline classified by
    keyword to a commodity. This is the prioritization layer: it tells
    the rest of Stage 1 which corridors the broader financial/news
    picture says are actually "hot" right now, so a local disruption
    or headline in that same commodity isn't just one isolated local
    read -- it's confirmed by an independent, different KIND of
    evidence (price action or national coverage, not another local
    sweep). See _stage1_detect_signals for how this is used."""
    return {s.commodity for s in macro_level_signals if s.commodity}


def _stage1_detect_signals(db: Session) -> list[MacroSignal]:
    """The gate. Returns every active macro signal found across the five
    leading-indicator channels (CME Globex price shock -- now also
    steel/crude/diesel/nat gas/livestock/dairy, not just grain, see
    SYMBOL_COMMODITIES -- GDELT NATIONAL news, GDELT LOCAL distress news
    near every monitored place, GFW ocean anomaly, and USACE river/port
    disruption across EVERY monitored corridor, not just grain -- a
    steel-corridor or port-congestion event can open this funnel same
    as a grain one, since this waterfall's filter/contact stages are
    generic, not grain-specific). An empty return means the waterfall
    stops here -- stages 2-4 never run and zero candidates get created.

    PRIORITIZATION: CME price shocks, the national GDELT sweep, and
    HigherGov contract awards are computed FIRST, since they're all
    macro-level reads -- see _hot_commodities_this_sweep. A contract
    award is treated as real, confirmed evidence here, same standing
    as a price move. Every local/regional signal (GDELT local, GDELT
    broad, USACE) whose commodity is ALSO in that hot set gets
    macro_corroborated=True -- not gated or skipped (GDELT is free and
    USACE is real disruption data, neither needs permission to run),
    just flagged as independently confirmed by a different kind of
    evidence, which Stage 2's hunts turn into a real confidence bonus
    instead of treating every hit as equally uncertain."""
    cme_signals = _cme_price_shock_signals(db)
    national_gdelt_signals = _gdelt_shock_signals(db)
    contract_signals = _highergov_contract_signals(db)
    hot = _hot_commodities_this_sweep(cme_signals + national_gdelt_signals + contract_signals)

    local_signals = _gdelt_local_distress_signals(db)
    broad_signals = _gdelt_broad_disruption_signals(db)
    usace_signals = _usace_disruption_signals(db)
    for s in local_signals + broad_signals + usace_signals:
        if s.commodity in hot:
            s.macro_corroborated = True

    return (
        cme_signals
        + national_gdelt_signals
        + contract_signals
        + local_signals
        + broad_signals
        + _gfw_anomaly_signals(db)
        + usace_signals
    )


def _active_vessel_corroboration(targets: list[GeoTarget]) -> str:
    """Actively checks for REAL, current vessel activity near every
    target that carries real coordinates (nationwide-sweep targets
    don't have any -- see GeoTarget), using the exact same confirmed
    Datalastic `/vessel_inradius` / VesselFinder bbox query
    telemetry/usace.py's AIS-queue channel already proved out -- not a
    second, unconfirmed way to ask "what's near this point."

    This replaces relying on SeaVantage/Datalastic/VesselFinder's own
    passive fetch() sweep for corroboration here: those adapters'
    fetch() methods are still unparameterized TODO stubs (see those
    files -- Import Genius and SeaVantage's own endpoints have no real
    filter wired in yet), so whatever they'd return isn't meaningfully
    tied to a specific disruption's location at all. Querying
    Datalastic/VesselFinder's CONFIRMED radius endpoint directly,
    centered on the actual GeoTarget, is a real, location-specific
    check instead.

    SeaVantage has no confirmed radius-search endpoint of its own (its
    file is still a TODO stub) -- not included here, only Datalastic/
    VesselFinder's already-proven query is reused, never a guessed one.

    Returns an empty string if no key is configured, there are no
    targets with real coordinates to check, or the live call fails --
    a failed/skipped check just means no corroboration this sweep, not
    an error, same posture as every other active-hunt path.
    """
    settings = get_settings()
    datalastic_key, vesselfinder_key = settings.datalastic_api_key, settings.vesselfinder_api_key
    if not (datalastic_key or vesselfinder_key):
        return ""

    real_targets = [t for t in targets if t.lat is not None and t.lon is not None]
    if not real_targets:
        return ""

    total_vessels = 0
    try:
        with httpx.Client(timeout=30) as client:
            for target in real_targets:
                if datalastic_key:
                    vessels = vessels_near_datalastic(client, datalastic_key, target.lat, target.lon)
                else:
                    vessels = vessels_near_vesselfinder(client, vesselfinder_key, target.lat, target.lon)
                total_vessels += len(vessels)
    except Exception:
        return ""

    return (
        f"active AIS check near {len(real_targets)} target location(s): {total_vessels} real vessel(s) "
        f"currently present -- live Datalastic/VesselFinder radius query, not passive history"
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
    ones from the parcel's own geometry. Import Genius candidates ALSO
    get checked for a trade link against any active port_congestion
    target (see _trade_linked_port_note) -- a shipment whose own
    manifest cleared through the congested port is linked by that
    transaction regardless of how far the business's address is from
    the dock, which is what actually catches a business the 60-mile
    geo radius alone would miss.
    """
    targets = targets or []
    corroboration_events: list[TelemetryEvent] = []
    for source in (
        TelemetrySource.SEAVANTAGE,
        TelemetrySource.DATALASTIC,
        TelemetrySource.VESSELFINDER,
        TelemetrySource.USACE,
        # News/ocean-anomaly corroboration -- if GDELT is ALSO carrying
        # fresh distress coverage, or GFW is ALSO seeing a fresh ocean
        # anomaly, this sweep, that's independent supporting evidence
        # for whatever candidate Stage 2 identifies, same as a vessel or
        # barge signal always was -- they just weren't counted before.
        TelemetrySource.GDELT,
        TelemetrySource.GFW_4WINGS,
    ):
        corroboration_events += _fresh_events(db, source, limit=10)

    if corroboration_events:
        sources_seen = sorted({e.source.value for e in corroboration_events})
        corroboration_note = f"{len(corroboration_events)} corroborating signals this sweep ({', '.join(sources_seen)})"
    else:
        corroboration_note = "no corroborating vessel/barge logistics signal this sweep"

    active_ais_note = _active_vessel_corroboration(targets)
    if active_ais_note:
        corroboration_note = f"{corroboration_note} | {active_ais_note}"

    identified: list[tuple[str, str, str, tuple[float, float] | None]] = []

    for event in _fresh_events(db, TelemetrySource.IMPORT_GENIUS, limit=25):
        name = event.payload.get("consignee_name") or event.payload.get("company_name")
        if name:
            # Two independent trade-link checks -- port of entry (which
            # port a shipment cleared through) and goods content (what
            # the shipment actually IS). A supermarket/wholesaler
            # importing Argentine beef gets caught by the second one
            # even if it clears through a port with no congestion at
            # all -- the link is the cargo itself, not the dock.
            port_link_note = _trade_linked_port_note(event.payload, targets)
            goods_link_note = _commodity_trade_linked_note(event.payload, targets)
            trade_link_note = f"{port_link_note}{goods_link_note}"
            note = f"{trade_link_note}{corroboration_note}" if trade_link_note else corroboration_note
            identified.append((str(name), f"telemetry_event:{event.id} [import_genius] {event.title}", note, None))

    for company_name, source_reference, regrid_note, coordinates in _identify_via_regrid(db, targets=targets):
        identified.append((company_name, source_reference, f"{corroboration_note} | {regrid_note}", coordinates))

    identified += _stage2b_apollo_industry_hunt(db, targets)
    identified += _stage2c_cobalt_industry_hunt(db, targets)

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


def _merge_cross_source_duplicates(
    identified: list[tuple[str, str, str, tuple[float, float] | None]],
) -> list[tuple[str, str, str, tuple[float, float] | None, bool]]:
    """Runs Interzoid fuzzy name-matching (same entity_matching utility
    stage 3/4 already use) across everything Stage 2 found THIS sweep,
    before any of it becomes a SiloCandidate.

    With three independent discovery paths now able to fire on one
    disruption -- Regrid/Import Genius (passive), the Apollo hunt, the
    Cobalt hunt -- the same real business can get surfaced twice under
    two different name spellings ("Three Rivers Steel Fab LLC" vs.
    "THREE RIVERS STEEL FABRICATION, INC"). Without this, that becomes
    two separate candidate rows for one real company instead of one
    higher-confidence row.

    The FIRST source's source_reference is kept as the merged row's
    identity, so re-running this sweep later still recognizes it as the
    same candidate; the second source's own reference and note are
    folded into the merged note rather than discarded, so nothing about
    the second discovery is lost, just not duplicated as its own row.

    When two or more independent sources land on the same business,
    that's genuine corroboration -- two unrelated databases agreeing a
    real company exists there, not the same database returning the same
    thing twice -- so the 5th element flags that `True`. This is what
    actually makes "independent sources agree" useful instead of just
    something you'd notice by eye in the notes: callers use it to score
    a cross-source-confirmed candidate higher than a single-source one.
    """
    interzoid_key = get_settings().interzoid_api_key
    merged: list[list] = []

    for name, source_ref, note, coordinates in identified:
        match = None
        for entry in merged:
            if entity_matching.names_match(interzoid_key, name, entry[0]):
                match = entry
                break
        if match is None:
            merged.append([name, source_ref, note, coordinates, False])
            continue

        other_source = _infer_origin_source(source_ref)
        other_label = other_source.value if other_source else "another source"
        match[2] = f"{match[2]} || CROSS-SOURCE CONFIRMED: also independently found via {other_label} -- {note}"
        match[3] = match[3] or coordinates
        match[4] = True

    return [tuple(entry) for entry in merged]


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
    identified = _merge_cross_source_duplicates(identified)

    created = 0
    dismissed = 0
    for company_name, trade_source_ref, corroboration_note, coordinates, cross_source_confirmed in identified:
        already = (
            db.query(SiloCandidate)
            .filter(SiloCandidate.silo == CME_MACRO_SILO, SiloCandidate.source_reference == trade_source_ref)
            .first()
        )
        if already:
            continue

        standing_delta, standing_dismiss, lending_note = _stage3_lending_appetite(db, company_name)
        revenue_delta, revenue_dismiss, revenue_note = _stage3b_revenue_range_filter(db, company_name)
        # +10 for two or more INDEPENDENT sources (e.g. Apollo + Cobalt)
        # landing on the same business -- real corroboration, not the
        # same source counted twice. +5 when the hunt that found it was
        # itself macro-corroborated (a CME shock or national GDELT
        # headline independently agreeing with the local/regional
        # signal that triggered the hunt -- see _hot_commodities_this_
        # sweep) -- smaller than the cross-source bonus since it
        # confirms the COMMODITY is active, not this specific company.
        cross_source_bonus = 10.0 if cross_source_confirmed else 0.0
        macro_corroboration_bonus = 5.0 if "MACRO-CORROBORATED" in corroboration_note else 0.0
        score_delta = standing_delta + revenue_delta + cross_source_bonus + macro_corroboration_bonus
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
                    "cross-source confirmed (+10 score)" if cross_source_confirmed else None,
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
    # Same prioritization pattern as _stage1_detect_signals: the
    # macro-level read (grain-filtered CME shocks, plus the national
    # GDELT sweep if it happens to classify as grain) comes first, and
    # any local/regional grain signal it agrees with gets flagged
    # macro_corroborated for Stage 2's hunts to score with confidence.
    cme_grain_signals = _cme_price_shock_signals(db, commodities={"grain"})
    hot = _hot_commodities_this_sweep(cme_grain_signals + _gdelt_shock_signals(db) + _highergov_contract_signals(db, commodities={"grain"}))

    local_signals = _gdelt_local_distress_signals(db, commodities={"grain"})
    broad_signals = _gdelt_broad_disruption_signals(db, commodities={"grain"})
    usace_signals = _usace_disruption_signals(db, commodities={"grain"})
    for s in local_signals + broad_signals + usace_signals:
        if s.commodity in hot:
            s.macro_corroborated = True

    signals = cme_grain_signals + local_signals + broad_signals + usace_signals
    if not signals:
        return {
            "signals_detected": 0,
            "signal_summary": "",
            "candidates_created": 0,
            "detail": "no active grain-price or river-corridor signal this sweep -- waterfall gated shut, zero candidates derived",
        }

    signal_summary = "; ".join(f"[{s.kind}] {s.description}" for s in signals[:5])

    created = 0
    geo_targets = _geo_targets_from_signals(signals)
    identify_results = _merge_cross_source_duplicates(
        _identify_via_regrid(db, targets=geo_targets)
        + _stage2b_apollo_industry_hunt(db, geo_targets)
        + _stage2c_cobalt_industry_hunt(db, geo_targets)
    )
    for company_name, source_reference, regrid_note, coordinates, cross_source_confirmed in identify_results:
        already = (
            db.query(SiloCandidate)
            .filter(SiloCandidate.silo == GRAIN_SILO, SiloCandidate.source_reference == source_reference)
            .first()
        )
        if already:
            continue

        notes = f"Gated by: {signal_summary} | {regrid_note}"
        if cross_source_confirmed:
            notes += " | cross-source confirmed"

        db.add(
            SiloCandidate(
                silo=GRAIN_SILO,
                company_name=company_name,
                source_reference=source_reference,
                notes=notes,
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
