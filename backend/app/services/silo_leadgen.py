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

import json
import logging
import math
import re
from datetime import date

import httpx
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import MasterLogStatus, SiloCandidateStatus, SiloName, TelemetrySource
from app.models.orm import MasterLogEntry, SiloCandidate, TelemetryEvent
from app.services import entity_matching, telemetry
from app.services.telemetry.apollo import ApolloAdapter
from app.services.telemetry.cobalt_intelligence import CobaltIntelligenceAdapter
from app.services.telemetry.gdelt import GDELTAdapter
from app.services.telemetry.usace import MONITORED_LOCKS, vessels_near_datalastic, vessels_near_vesselfinder

logger = logging.getLogger("brainboard.silo_leadgen")

SOURCE_TO_SILOS: dict[TelemetrySource, list[SiloName]] = {
    TelemetrySource.CME_GLOBEX: [SiloName.TARIFF_SILO],
    # Widened from Oil & Gas Refining alone -- SeaVantage's real
    # product (confirmed against seavantage.com) is general container/
    # cargo dwell-time tracking, not an oil-and-gas-specific feed, and
    # its demurrage signal (see _demurrage_risk_note) is just as
    # relevant to a stuck e-commerce container or a tariff-silo import
    # as an oil/gas shipment.
    TelemetrySource.SEAVANTAGE: [SiloName.OIL_GAS_REFINING_SILO, SiloName.ECOMMERCE_FULFILLMENT_SILO, SiloName.TARIFF_SILO],
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
    """HONESTY NOTE on the HigherGov keys below: the HigherGov adapter
    currently hits `/opportunity/` -- open solicitations, not a
    confirmed awards/recipients endpoint (see telemetry/highergov.py's
    own TODO) -- so most of these payloads won't carry a named winner
    at all yet, and this falls back to event.title (the opportunity's
    own title, not a company) same as always. These keys are defensive
    best-effort for the plans/historical-opportunity records that DO
    carry a named awardee, and for whenever that adapter is pointed at
    a real awards endpoint -- not a claim that a named company is
    reliably present today."""
    for key in (
        "company_name", "consignee_name", "recalling_firm", "entity_name", "debtor_name", "symbol",
        "awardee_name", "recipient_name", "vendor_name", "contractor_name", "awardee",
    ):
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
    # Set only by _llm_classify_news-backed signals (a genuinely novel
    # industry, not one of the hand-wired commodities) -- the hunt
    # keywords/term travel WITH the signal instead of being looked up
    # from the static _COMMODITY_APOLLO_KEYWORDS/_COMMODITY_COBALT_
    # SEARCH_TERMS dicts, since a newly-discovered industry isn't in
    # them. None for every hand-wired commodity, which keeps using the
    # static dicts as before.
    dynamic_apollo_keywords: list[str] | None = None
    dynamic_cobalt_term: str | None = None


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
    # These 4 were added for smaller/mid-size port coverage but never
    # got added here -- a real gap, this trade-link match only ever
    # worked for the original 4 ports until now.
    "Port of Savannah anchorage": ("savannah",),
    "Port of Charleston anchorage": ("charleston",),
    "Port of Oakland anchorage": ("oakland",),
    "Port of Tacoma anchorage (Northwest Seaport Alliance)": ("tacoma", "seattle", "northwest seaport"),
    # New port expansion (toward comprehensive US port coverage).
    "Port of Virginia anchorage (Norfolk)": ("norfolk", "hampton roads", "port of virginia"),
    "Port of Baltimore anchorage": ("baltimore",),
    "Port of Jacksonville anchorage": ("jacksonville",),
    "Port of Corpus Christi anchorage": ("corpus christi",),
    "Port of Beaumont anchorage": ("beaumont",),
    "Port of Mobile anchorage": ("mobile",),
    "Port of Plaquemines anchorage": ("plaquemines",),
    "Port of San Juan anchorage": ("san juan", "puerto rico"),
    # Great Lakes AIS-only ports -- missed entirely until now, same
    # gap as Savannah/Charleston/Oakland/Tacoma above.
    "Duluth-Superior Harbor (Lake Superior)": ("duluth", "superior"),
    "Port of Cleveland (Lake Erie)": ("cleveland",),
    "Port of Toledo (Lake Erie)": ("toledo",),
    "Burns Harbor / Gary anchorage (Lake Michigan)": ("burns harbor", "gary"),
    "Detroit River anchorage (Detroit, MI)": ("detroit",),
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
    # "copper" added -- the April/June 2026 Section 232 revision
    # explicitly expanded the steel/aluminum regime to copper too
    # (confirmed via direct research, not a guess).
    "steel_coal": ("steel", "coal", "iron ore", "coke", "aluminum", "copper"),
    # Apparel/footwear -- one of the most consistently named tariff-
    # exposed categories in current coverage (confirmed via direct
    # research). Kept as ONE commodity, not split into wholesaler/
    # retailer tiers like food -- the financing stories across that
    # chain aren't differentiated enough here to justify the extra
    # tiers, same reasoning furniture_wood already uses.
    # Previously MISSING entirely -- aerospace_defense had no goods-
    # keyword entry at all, meaning Import Genius cargo-content matching
    # never worked for it even though the commodity existed. Widened
    # beyond aerospace specifically to the full defense-contracting
    # space -- HigherGov is a channel for ALL of DoD contracting
    # (Army/Navy/Marine Corps as much as Air Force), not just aircraft.
    "aerospace_defense": ("aerospace", "aircraft", "avionics", "missile", "satellite", "naval", "shipbuilding", "ammunition", "tactical vehicle", "defense electronics"),
    "apparel": ("apparel", "clothing", "footwear", "garment", "textile"),
    # Auto parts/components -- confirmed Section 301 legacy list
    # coverage on motor vehicles/industrial machinery from China.
    # Deliberately NOT tied to Mexico despite deep US-auto supply-
    # chain integration -- CUSMA-compliant auto parts are mostly
    # exempt, so forcing Mexico in as "affected" here would misstate
    # the real exemption, not just approximate it.
    "auto_parts": ("auto parts", "automotive parts", "vehicle components", "auto components"),
    # Added specifically because HigherGov's own classifier
    # (_classify_commodity_from_text) returned None for real
    # construction/renovation contract headlines -- government
    # construction/infrastructure/facility-renovation bids are one of
    # the most common real contract-award categories there is, and
    # nothing here was catching them. Shares real cargo-content overlap
    # with steel_coal (steel) and furniture_wood (lumber) on purpose --
    # a renovation contractor's own buying pattern genuinely crosses
    # into both.
    "construction_contractor": ("construction materials", "building materials", "lumber", "steel", "renovation supplies"),
    # Commercial/industrial construction is a genuinely distinct
    # population from residential/general renovation above -- bigger
    # jobs, bigger steel/concrete volume, and a real, well-documented
    # MCA story of its own (progress-billing/retainage cash-flow gaps
    # between a draw request and actual payment). Deliberately
    # multi-word, specific goods phrases, not bare "steel"/"concrete" --
    # those would just collide with construction_contractor's own
    # generic goods keywords above with no way to tell which commodity
    # a shipment actually belongs to.
    "commercial_construction": ("structural steel", "rebar", "ready-mix concrete", "commercial building materials"),
    # Crane rental/operator companies and heavy-equipment dealers --
    # real import cargo content in its own right (tower cranes,
    # excavators, and heavy construction machinery are commonly
    # imported, notably from Japan/South Korea/China -- see
    # _ASIA_RELEVANT_COMMODITIES below), distinct from the construction
    # MATERIALS a contractor buys.
    "crane_heavy_equipment": ("crane", "tower crane", "excavator", "bulldozer", "heavy equipment", "construction machinery"),
    "poultry_producer": ("poultry", "chicken", "broiler", "live poultry"),
    # One tier downstream of petrochemical_producer -- finished
    # specialty-chemical goods, not raw resin/feedstock, which is
    # already covered by petrochemical's own goods keywords above.
    "chemical_products_manufacturer": ("detergent", "soap", "cleaning product", "coating", "adhesive"),
    "solar_installer": ("solar panel", "solar module", "photovoltaic", "solar cell"),
    "solar_manufacturer": ("solar panel", "solar module", "photovoltaic", "solar cell"),
    "lumber_wholesaler": ("lumber", "softwood lumber", "dimensional lumber", "sawn timber"),
    "lumber_producer": ("lumber", "softwood lumber", "dimensional lumber", "sawn timber"),
    "rare_earth_importer": ("rare earth", "neodymium", "rare earth magnet", "rare earth element"),
    "ev_charging_installer": ("ev charger", "charging station", "evse", "electric vehicle charging"),
    # The actual e-commerce BRAND/seller, not the fulfillment/3PL
    # company that serves it (ecommerce_logistics above) -- same
    # physical cargo content either way (a seller's own container of
    # private-label goods looks identical to a 3PL's), the difference
    # is entirely in which population Apollo/Cobalt searches for.
    "ecommerce_seller": ("consumer goods", "apparel", "footwear", "toys", "electronics", "housewares", "general merchandise"),
    # "oil"/"natural gas"/"lng"/"petroleum" added so a crude/NG cargo
    # shipment -- or a geopolitical oil/gas headline -- classifies here
    # too, not just a finished-plastics shipment.
    "petrochemical": ("petrochemical", "resin", "plastic", "polymer", "chemical", "oil", "natural gas", "lng", "petroleum"),
    # The seller side of the petrochemical complex -- NGL processors
    # and intermediate chemical/resin producers who SELL what the
    # "petrochemical" tier above BUYS as feedstock. Same goods content
    # either way; the trade_side split (see _COMMODITY_TRADE_SIDE) is
    # what actually tells them apart.
    "petrochemical_producer": ("petrochemical", "resin", "plastic", "polymer", "chemical", "oil", "natural gas", "lng", "petroleum"),
    # Same cargo-content keywords across all four downstream tiers --
    # the physical commodity in a shipment doesn't change by which TIER
    # of the chain happens to be moving it; see SYMBOL_COMMODITIES and
    # _COMMODITY_APOLLO_KEYWORDS below for how these four get split
    # into their own distinct hunts.
    "meat_processor": ("beef", "meat", "poultry", "dairy", "cheese", "pork"),
    "food_wholesaler": ("beef", "meat", "poultry", "dairy", "cheese", "pork"),
    "food_retailer": ("beef", "meat", "poultry", "dairy", "cheese", "pork"),
    "food_service": ("beef", "meat", "poultry", "dairy", "cheese", "pork"),
    "livestock_producer": ("beef", "cattle", "livestock", "meat", "pork", "dairy"),
    "fertilizer": ("potash", "fertilizer", "urea", "ammonia", "phosphate", "muriate of potash"),
    # General retail/consumer-goods cargo content -- what actually
    # answers "who's importing the stuff that's stuck behind a tariff
    # or a container backlog," distinct from ecommerce_logistics'
    # land-use/Apollo keywords below
    # (which target the FULFILLMENT/3PL operators, not the importers).
    "ecommerce_logistics": ("consumer goods", "apparel", "footwear", "toys", "electronics", "housewares", "general merchandise"),
    # Component-level cargo content, distinct from ecommerce_logistics'
    # generic "electronics" (finished consumer goods) -- this is what
    # answers "who's actually importing/moving the chips," the same
    # transactional logic as the beef/fertilizer goods matches.
    "semiconductor_distributor": ("semiconductor", "integrated circuit", "microchip", "electronic component", "wafer"),
    "electronics_manufacturer": ("semiconductor", "integrated circuit", "microchip", "electronic component", "circuit board"),
    "furniture_wood": ("furniture", "wood", "lumber", "cabinetry", "wood products", "plywood"),
    # Latin America trickle-down chain, generalized beyond just beef
    # (meat_processor/food_wholesaler/food_retailer/food_service
    # already exist for that) -- produce and liquor/spirits are the
    # other two large, real Latin-America-sourced import categories.
    "produce_wholesaler": ("produce", "fruit", "vegetable", "fresh produce"),
    "produce_retailer": ("produce", "fruit", "vegetable", "fresh produce"),
    "liquor_distributor": ("liquor", "spirits", "rum", "tequila", "wine", "beer"),
    "liquor_retailer": ("liquor", "spirits", "rum", "tequila", "wine", "beer"),
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


# Standard bill-of-lading country-of-origin field name isn't confirmed
# against Import Genius's live schema -- same defensive multi-key
# posture as _extract_address/_extract_port_of_entry. Also checks
# SeaVantage's real milestone-tracking field names (origin port /
# empty-equipment-dispatch location -- see telemetry/seavantage.py's
# confirmed real product scope) since a company's OWN container
# milestones are a second, independent way to confirm where a
# shipment actually originated, not just whatever Import Genius's
# customs field says -- genuinely useful when a company runs MIXED
# sourcing (some containers from one country, some from another) and
# a single customs record alone wouldn't show the full picture.
_COUNTRY_OF_ORIGIN_KEYS = (
    "country_of_origin", "origin_country", "shipper_country", "exporter_country", "country",
    "pol_country", "origin_port_country", "empty_dispatch_country",
)


def _extract_country_of_origin(payload: dict) -> str | None:
    for key in _COUNTRY_OF_ORIGIN_KEYS:
        value = payload.get(key)
        if value:
            return str(value).lower()
    return None


# Known active US tariff actions and their real, documented trade-
# diversion beneficiaries -- maintained as a standing policy fact, same
# posture as MONITORED_LOCKS or SYMBOL_COMMODITIES: tariff policy is
# public and changes rarely, so this is something to UPDATE when it
# actually changes, not something to "discover" fresh from a news
# query every sweep. Section 301 tariffs on Chinese furniture are the
# textbook case here: Vietnam (and, to a lesser extent, Malaysia/
# Indonesia) absorbed a large, well-documented share of the sourcing
# volume that shifted away from China as a direct result -- a real
# reported pattern, not a guess.
#
# Section 232 steel/aluminum tariffs (Canada being the largest single
# source of both US steel and aluminum imports) are the second real
# entry here -- deliberately with NO diversion-beneficiary country
# listed. Unlike furniture-to-Vietnam, there isn't one single country
# that's absorbed Canada's lost steel/aluminum share at anywhere near
# that same documented scale -- forcing a beneficiary country here
# would be a guess dressed up as a fact. The expansion side of this
# tariff is still covered, just through the mechanism already built
# for it: steel_coal's trade_side is "producer" (see
# _COMMODITY_TRADE_SIDE), so the standing tariff signal below drives
# the generic nationwide Apollo/Cobalt hunt for domestic steel mills/
# fabricators/metal service centers, framed "expansion" -- that's the
# real expansion story here (domestic producers gaining price
# advantage), not a specific alternative import source.
#
# Four entries added from direct research into the actual July 2026
# Section 301 forced-labor action and China's stacked legacy tariffs
# (confirmed, not guessed -- though even professional trade-law
# trackers disagree on some specifics, like Vietnam's exact rate
# tier, so treat these as directionally right, not Federal-Register-
# exact):
#   - "russia" added to steel_coal -- Russian aluminum specifically
#     carries a 200% ad valorem rate, far more extreme and
#     differentiated than the general Canada/Section 232 exposure.
#     Same commodity, same mechanism, just a second real affected
#     country, not a replacement for Canada.
#   - "apparel"/"auto_parts"/"semiconductor_distributor" added with
#     China -- apparel/footwear and auto parts/industrial machinery
#     are among the most consistently named tariff-exposed categories
#     in current coverage; semiconductors reportedly face an
#     especially high stacked China rate (least-confirmed figure of
#     the four, flagged accordingly).
#   - Deliberately NO diversion-beneficiary country for apparel --
#     unlike furniture-to-Vietnam, Bangladesh/Cambodia/Vietnam (the
#     realistic alternative sourcing countries) are ALSO tariffed
#     under the broad 10%/12.5% action, just at a lower rate than
#     China's stacked total. That's a relative-cost-advantage story,
#     not a clean untaxed escape -- forcing one of them in as
#     "beneficiary" here would overstate it.
#   - HONEST LIMIT on apparel/semiconductor_distributor specifically:
#     this check is shipment-level country of origin, not component-
#     level. A device/garment assembled in Vietnam from Chinese
#     components still carries real Chinese-content tariff exposure
#     that this check cannot see -- it will under-detect, not
#     over-claim, which is the safer direction to be wrong in, but
#     it's a real gap, not a solved problem.
# Two new entries below are deliberately keyed to the PRODUCER side
# (solar_manufacturer, lumber_producer), not the consumer side
# (solar_installer, lumber_wholesaler) -- same goods keywords either
# way, so the per-shipment check (_tariff_trade_diversion_note) still
# correctly flags a China-sourced panel or Canada-sourced softwood
# shipment as "distress" regardless of which key it matches on. But
# the STANDING signal (_tariff_policy_signals) reads whichever key is
# here and defaults it to "expansion" -- keying it to the producer
# side means that default correctly drives the nationwide hunt toward
# domestic manufacturers/sawmills (who genuinely benefit), not
# installers/wholesalers (who are squeezed). Same real research-
# backed confidence as the other entries: Chinese solar cells
# reportedly carry a ~50% stacked rate (least-confirmed of the two,
# flagged same as the earlier semiconductor entry); Canada softwood
# lumber countervailing/anti-dumping duties are one of the longest-
# running, most specific US-Canada trade disputes there is, separate
# from the general steel/aluminum Section 232 action.
_TARIFF_AFFECTED_COUNTRIES: dict[str, tuple[str, ...]] = {
    "furniture_wood": ("china",),
    "steel_coal": ("canada", "russia"),
    "apparel": ("china",),
    "auto_parts": ("china",),
    "semiconductor_distributor": ("china",),
    "solar_manufacturer": ("china",),
    # Most private-label/Amazon-FBA inventory is China-sourced -- the
    # dominant real pattern for this population, not a guess. No
    # diversion-beneficiary country forced here either, same honesty
    # as apparel: sellers who've shifted to Vietnam/India are real,
    # but not a clean single-country win at furniture-to-Vietnam's
    # scale.
    "ecommerce_seller": ("china",),
    "lumber_producer": ("canada",),
}
_TARIFF_DIVERSION_BENEFICIARY_COUNTRIES: dict[str, tuple[str, ...]] = {
    "furniture_wood": ("vietnam", "malaysia", "indonesia"),
}


def _tariff_trade_diversion_note(payload: dict) -> tuple[str, str | None]:
    """The Argentina-beef producer/consumer split, applied to tariff
    policy instead of a price move or a war headline. A tariff creates
    a THREE-WAY split, not a two-way one:
    1. An importer still sourcing from the TARIFFED country is now
       paying it -- margin squeeze or a forced re-sourcing cost, a
       real bridge-capital need regardless of underlying health.
    2. An importer who shifted sourcing to a real, documented
       diversion-beneficiary country is scaling a GROWING import flow
       -- a real expansion-capital need.
    3. A domestic producer of the competing good benefits too, but
       Import Genius has no domestic-shipment record to check a
       country of origin against -- that population is covered
       separately by the generic nationwide Apollo/Cobalt hunt for
       this commodity (see _tariff_policy_signals, framed "expansion"
       by default).

    This is a STANDING per-shipment check, independent of whether any
    MacroSignal fired this sweep -- tariff policy doesn't "shock" the
    way a price move does, it's a persistent condition, so every fresh
    Import Genius shipment matching a tariff-tracked commodity's goods
    content gets checked against its own country of origin every
    sweep, not gated on an active signal.

    Returns (note, signal_type) -- ("", None) when the shipment's
    goods don't match any tariff-tracked commodity, or its country of
    origin is neither the tariffed country nor a documented diversion
    beneficiary (true for most countries and most shipments) -- never
    a dismiss, just no claim either way for that specific shipment.
    """
    description = _extract_goods_description(payload)
    country = _extract_country_of_origin(payload)
    if not description or not country:
        return "", None

    for commodity in {**_TARIFF_AFFECTED_COUNTRIES, **_TARIFF_DIVERSION_BENEFICIARY_COUNTRIES}:
        keywords = _COMMODITY_GOODS_KEYWORDS.get(commodity, ())
        if not keywords or not any(k in description for k in keywords):
            continue

        affected = _TARIFF_AFFECTED_COUNTRIES.get(commodity, ())
        beneficiary = _TARIFF_DIVERSION_BENEFICIARY_COUNTRIES.get(commodity, ())
        if any(c in country for c in affected):
            return (
                f"TARIFF-AFFECTED SOURCING: shipment's country of origin '{country}' is subject to the "
                f"active {commodity} tariff -- likely margin squeeze or re-sourcing cost, a real "
                f"bridge-capital need regardless of underlying business health",
                "distress",
            )
        if any(c in country for c in beneficiary):
            return (
                f"TARIFF-DIVERSION WINNER: shipment's country of origin '{country}' is a real, documented "
                f"trade-diversion beneficiary for {commodity} (absorbing volume shifted away from the "
                f"tariffed country) -- scaling import volume, a real expansion-capital need",
                "expansion",
            )
    return "", None


def _tariff_policy_signals() -> list[MacroSignal]:
    """A standing policy fact, not an event -- tariff policy doesn't
    expire on a date or need a fresh news hit to still be true, it
    stays active until _TARIFF_AFFECTED_COUNTRIES is updated to reflect
    a real policy change. Drives the generic nationwide Apollo/Cobalt
    hunt for this commodity's domestic manufacturers/distributors
    (framed "expansion" by default -- the broad story here is import-
    substitution/trade-diversion growth, not distress) while the
    PER-SHIPMENT country-of-origin check (_tariff_trade_diversion_note)
    refines or overrides that framing for any specific Import Genius
    candidate whose actual country of origin is known."""
    return [
        MacroSignal(
            kind="tariff_policy",
            description=f"Active US tariff policy affecting {commodity} sourcing from {', '.join(countries)}",
            source_reference=f"tariff_policy:{commodity}",
            commodity=commodity,
            signal_type="expansion",
        )
        for commodity, countries in _TARIFF_AFFECTED_COUNTRIES.items()
    ]


def _detect_mixed_sourcing_companies(db: Session) -> dict[str, str]:
    """What if the same company shows up as BOTH a tariffed-country
    importer AND a diversion-beneficiary importer this sweep -- i.e.
    it's actively mid-transition, running containers from China and
    Vietnam at the same time. That's not a weaker signal than either
    side alone, it's a SHARPER one: a company caught mid-
    diversification has its own distinct capital need (funding the
    transition itself -- qualifying new factories, new supplier
    contracts, temporarily double-paying for overlapping inventory
    during the switchover) that neither a pure-distress nor a pure-
    expansion read captures on its own.

    Scans every fresh Import Genius shipment this sweep ONCE, groups
    by company name, and flags any company whose shipments show BOTH
    a "distress" (tariff-affected) and an "expansion" (diversion-
    beneficiary) classification across different shipments -- this is
    why it's a separate pre-pass rather than folded into the per-
    shipment check: detecting "mixed" requires seeing ALL of a
    company's shipments together, not just one at a time.

    Scans BOTH Import Genius (customs manifest) and SeaVantage (real
    per-container milestone tracking, see telemetry/seavantage.py) --
    a company running mixed sourcing might show up with one country on
    its customs record and a different one on a container's own
    milestone history; checking both sources catches a split that
    either one alone could miss.

    HONESTY NOTE on precision: this groups by company NAME only, the
    same name-matching posture as every other cross-shipment check in
    this module -- a guaranteed per-container join (confirming the
    SAME company's containers specifically, not just a name collision
    across two different providers) would need SeaVantage's exact
    schema confirmed against live docs first; see telemetry/
    seavantage.py's own TODO. Until then, this is a reasonable
    approximation, not a guaranteed fact.

    Returns {company_name: note} for flagged companies only."""
    seen: dict[str, set[str]] = {}
    for source in (TelemetrySource.IMPORT_GENIUS, TelemetrySource.SEAVANTAGE):
        for event in _fresh_events(db, source, limit=25):
            name = event.payload.get("consignee_name") or event.payload.get("company_name")
            if not name:
                continue
            _, signal_type = _tariff_trade_diversion_note(event.payload)
            if signal_type:
                seen.setdefault(str(name), set()).add(signal_type)

    return {
        name: (
            f"MIXED SOURCING: '{name}' shows BOTH tariff-affected AND trade-diversion-beneficiary "
            f"shipments this sweep -- actively mid-transition between suppliers, a real capital need "
            f"to fund the diversification itself, not just a distress or expansion read alone"
        )
        for name, types in seen.items()
        if {"distress", "expansion"} <= types
    }


# HONESTY NOTE: before this, nothing in this file ever looked at a
# shipment's actual SIZE -- every check so far (tariff, Latin America,
# Asia sourcing, mixed sourcing, even the cargo-volume revenue proxy
# above) is a binary match or a plain COUNT of shipments, never the
# real weight/quantity on a given bill of lading. That's the honest
# answer to "how do we tell who has the biggest volume": until now, we
# couldn't -- a company with one 40,000 lb bulk potash shipment and one
# with a 400 lb sample shipment would have scored identically. These
# keys are the same defensive, multi-plausible-name posture as every
# other unconfirmed Import Genius field in this file (see
# _GOODS_DESCRIPTION_KEYS/_COUNTRY_OF_ORIGIN_KEYS) -- a real bill of
# lading typically carries SOME weight/quantity field, exact name
# unconfirmed against live docs.
_SHIPMENT_WEIGHT_KEYS = (
    "weight_kg", "gross_weight_kg", "gross_weight", "weight_lbs", "weight", "net_weight", "quantity",
)


def _extract_shipment_weight(payload: dict) -> float | None:
    for key in _SHIPMENT_WEIGHT_KEYS:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


# Import Genius's own support docs confirm a real field here --
# "Container Numbers: unique identification numbers for the shipping
# containers" -- but a single bill of lading can cover MULTIPLE
# containers (consolidated shipments are common), so this field is
# very likely a multi-value one (a list, or a delimited string), not a
# single ID. Counting records the way _cargo_volume_revenue_proxy does
# would undercount real container volume whenever a shipment is
# consolidated -- this parses the field directly for the actual number
# of container IDs listed instead. SeaVantage needs none of this: its
# real, confirmed product tracks at the container level natively, one
# record IS one container, so a matching SeaVantage event always
# counts as exactly 1 container, no parsing required.
_CONTAINER_NUMBER_KEYS = ("container_numbers", "container_number", "container_ids", "container_id")
_CONTAINER_ID_DELIMITERS = (",", ";", "/")


def _extract_container_count(payload: dict) -> int | None:
    for key in _CONTAINER_NUMBER_KEYS:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            count = len([v for v in value if v])
            if count:
                return count
            continue
        text = str(value).strip()
        if not text:
            continue
        for delimiter in _CONTAINER_ID_DELIMITERS:
            text = text.replace(delimiter, ",")
        ids = [part.strip() for part in text.split(",") if part.strip()]
        if ids:
            return len(ids)
    return None


# Container-count volume only means what it claims to mean for
# commodities that genuinely move IN containers. Potash, grain, raw
# petrochemical feedstock, and steel/coal/iron ore overwhelmingly ship
# as BULK cargo -- loose in a bulk carrier's hold, never touching a
# container at all (see "The Fertilizer Signal" brief) -- so counting
# containers for those would systematically miss most of their real
# volume, not just undercount it. These two sets are deliberately
# conservative: left OUT entirely (crane_heavy_equipment, which ships
# breakbulk/RoRo, too large for a standard container; raw lumber,
# which commonly ships break-bulk) rather than guessed into either
# bucket, same honesty posture as every other classification here.
_CONTAINERIZED_GOODS_COMMODITIES = frozenset(
    {"furniture_wood", "apparel", "electronics_manufacturer", "semiconductor_distributor", "ecommerce_seller", "auto_parts"}
)
_BULK_CARGO_COMMODITIES = frozenset({"fertilizer", "grain", "petrochemical", "petrochemical_producer", "steel_coal"})


@dataclass
class _VolumeRanking:
    total_weight: float = 0.0
    weight_rank: int | None = None
    total_containers: int = 0
    container_rank: int | None = None


def _rank_importers_by_volume(db: Session, commodity: str) -> dict[str, _VolumeRanking]:
    """Answers "who actually has the biggest volume" for one commodity,
    by TWO independent real measures -- actual shipped weight/quantity
    (the right unit for a bulk commodity like potash or grain) and
    actual container count (the right unit for a containerized-goods
    commodity like furniture or apparel) -- instead of a plain shipment
    count, which can't tell a company with fewer/bigger shipments
    apart from one with more/smaller ones (see
    _cargo_volume_revenue_proxy above).

    Scans fresh Import Genius + SeaVantage shipments matching this
    commodity's own goods keywords (same classification _tariff_trade_
    diversion_note and friends already use), groups by company name,
    and sums both real weight and real container count per company --
    one pass over the same events, not two separate scans. A shipment
    with no extractable weight/container field simply contributes
    nothing to that specific total; it's never treated as a zero that
    would misleadingly look like "smallest."

    Returns {company_name: _VolumeRanking} -- weight_rank/
    container_rank are None for a company never seen with a real value
    for that specific measure, same "unranked, not ranked last"
    posture as before. Which of the two ranks actually MEANS anything
    for this commodity is decided by the caller (_volume_rank_note),
    using _CONTAINERIZED_GOODS_COMMODITIES/_BULK_CARGO_COMMODITIES --
    this function always computes both, since the cost of tracking an
    extra running total is negligible next to the cost of a second
    full event scan."""
    keywords = _COMMODITY_GOODS_KEYWORDS.get(commodity, ())
    rankings: dict[str, _VolumeRanking] = {}
    if keywords:
        for source in (TelemetrySource.IMPORT_GENIUS, TelemetrySource.SEAVANTAGE):
            for event in _fresh_events(db, source, limit=50):
                description = _extract_goods_description(event.payload)
                if not description or not any(k in description for k in keywords):
                    continue
                name = _company_name_from_event(event)
                entry = rankings.setdefault(name, _VolumeRanking())

                weight = _extract_shipment_weight(event.payload)
                if weight is not None:
                    entry.total_weight += weight

                if source == TelemetrySource.SEAVANTAGE:
                    entry.total_containers += 1  # one real container per record, by SeaVantage's own product design
                else:
                    containers = _extract_container_count(event.payload)
                    if containers is not None:
                        entry.total_containers += containers

    by_weight = sorted((n for n, e in rankings.items() if e.total_weight > 0), key=lambda n: rankings[n].total_weight, reverse=True)
    for rank, name in enumerate(by_weight, start=1):
        rankings[name].weight_rank = rank

    by_containers = sorted((n for n, e in rankings.items() if e.total_containers > 0), key=lambda n: rankings[n].total_containers, reverse=True)
    for rank, name in enumerate(by_containers, start=1):
        rankings[name].container_rank = rank

    return rankings


# Top 3 by real volume -- not top 1 only, since several real importers
# can legitimately be close together at the top of a thin market
# (potash/fertilizer realistically has far fewer real importers than,
# say, apparel). Smaller than BIG-TICKET CONFIRMED's +20 and bigger
# than the cargo-volume proxy's +10 -- this is a real, ranked fact
# about actual shipped weight or container count, a stronger claim
# than the proxy's plain shipment count, but still not a confirmed
# revenue figure.
_VOLUME_RANK_TOP_N = 3
_VOLUME_RANK_SCORE_BONUS = 14.0


def _volume_rank_note(commodity: str, company_name: str, rankings: dict[str, "_VolumeRanking"]) -> str:
    """Picks whichever of the two real measures actually means
    something for this commodity's physical shipping mode --
    container count for a _CONTAINERIZED_GOODS_COMMODITIES member,
    shipped weight for a _BULK_CARGO_COMMODITIES member. A commodity in
    neither set gets no volume note at all -- genuinely unknown which
    unit would mean anything for it, so no claim, not a guess. Returns
    "" when company_name isn't ranked top-N on the relevant measure."""
    entry = rankings.get(company_name)
    if not entry:
        return ""

    if commodity in _CONTAINERIZED_GOODS_COMMODITIES:
        if entry.container_rank is None or entry.container_rank > _VOLUME_RANK_TOP_N:
            return ""
        companies_seen = sum(1 for e in rankings.values() if e.container_rank is not None)
        return (
            f"LARGEST VOLUME IMPORTER: '{company_name}' ranks #{entry.container_rank} of {companies_seen} real "
            f"{commodity} importers by actual container count this sweep ({entry.total_containers:,} containers) -- "
            f"a measured volume fact, not a shipment-count guess"
        )

    if commodity in _BULK_CARGO_COMMODITIES:
        if entry.weight_rank is None or entry.weight_rank > _VOLUME_RANK_TOP_N:
            return ""
        companies_seen = sum(1 for e in rankings.values() if e.weight_rank is not None)
        return (
            f"LARGEST VOLUME IMPORTER: '{company_name}' ranks #{entry.weight_rank} of {companies_seen} real "
            f"{commodity} importers by actual shipped weight this sweep ({entry.total_weight:,.0f}) -- a measured "
            f"volume fact, not a shipment-count guess"
        )

    return ""


# The Argentina-beef chain, generalized to its own standing category
# instead of only ever firing off a CME livestock price shock -- a
# business with heavy, real import volume from a major Latin America
# trade partner is a scaling operation on its own, whether or not
# today's cattle futures moved. Covers the two NEW categories
# (produce, liquor/spirits) plus the EXISTING beef-chain tiers, so an
# Argentina-sourced beef shipment still gets this standing confirmation
# even on a day with no CME price shock at all.
_LATIN_AMERICA_COUNTRIES = (
    "mexico", "brazil", "argentina", "colombia", "peru", "chile", "ecuador",
    "guatemala", "costa rica", "honduras", "dominican republic", "panama",
)
_LATIN_AMERICA_RELEVANT_COMMODITIES = (
    "produce_wholesaler", "produce_retailer", "liquor_distributor", "liquor_retailer",
    "meat_processor", "food_wholesaler", "food_retailer", "food_service",
)


def _latin_america_sourcing_note(payload: dict) -> tuple[str, str | None]:
    """A STANDING per-shipment check, same posture as
    _tariff_trade_diversion_note -- doesn't need any MacroSignal to
    have fired this sweep, since "this company does heavy business
    with a major Latin America trade partner" is true independent of
    today's news or price action. Always "expansion" when it fires --
    unlike the tariff check, there's no two-sided distress/expansion
    split here, just confirmation that a real, scaling import-driven
    growth business exists.

    Returns ("", None) when the shipment's goods don't match any
    tracked category, or its country of origin isn't a major Latin
    America trade partner -- never a dismiss, just no claim for that
    shipment."""
    description = _extract_goods_description(payload)
    country = _extract_country_of_origin(payload)
    if not description or not country:
        return "", None
    if not any(c in country for c in _LATIN_AMERICA_COUNTRIES):
        return "", None

    for commodity in _LATIN_AMERICA_RELEVANT_COMMODITIES:
        keywords = _COMMODITY_GOODS_KEYWORDS.get(commodity, ())
        if keywords and any(k in description for k in keywords):
            return (
                f"LATIN AMERICA SOURCING CONFIRMED: shipment's country of origin '{country}' is a major "
                f"Latin America trade partner for {commodity} -- heavy, real import volume from this "
                f"region, a scaling operation that needs capital to keep growing, not a distress read",
                "expansion",
            )
    return "", None


def _latin_america_import_signals() -> list[MacroSignal]:
    """The standing macro-level counterpart to
    _latin_america_sourcing_note -- same posture as
    _tariff_policy_signals. Only generates signals for the two NEW
    categories (produce, liquor/spirits); the existing beef-chain
    tiers already have their own CME-price-driven signal generation
    (_cme_price_shock_signals) and shouldn't get a second, competing
    always-on "expansion" signal that could blur that existing
    distress/expansion framing. The per-shipment check above still
    covers beef-chain shipments regardless."""
    return [
        MacroSignal(
            kind="latin_america_sourcing",
            description=f"Standing Latin America import-volume signal for {commodity}",
            source_reference=f"latin_america_sourcing:{commodity}",
            commodity=commodity,
            signal_type="expansion",
        )
        for commodity in ("produce_wholesaler", "produce_retailer", "liquor_distributor", "liquor_retailer")
    ]


# HONEST GAP THIS FIXES: _TARIFF_AFFECTED_COUNTRIES tracks exactly one
# Asian country -- China -- across apparel/auto_parts/
# semiconductor_distributor/solar_manufacturer/ecommerce_seller, even
# though the real, documented sourcing base for every one of those
# categories is far wider than China alone (Vietnam/Bangladesh/
# Cambodia/India for apparel; Japan/South Korea/Taiwan/India for auto
# parts and electronics; Taiwan/South Korea/Japan for semiconductors --
# arguably a BIGGER real source than China for that one). Latin America
# got its own 12-country standing-sourcing category; Asia, which is
# genuinely where most of what the US imports actually comes from, had
# nothing analogous -- a real, asymmetric blind spot, not a close call.
#
# Deliberately NOT a "diversion beneficiary" framing like furniture-to-
# Vietnam -- that implies a specific tariff-driven shift away from one
# named country. This is simpler and more honest: these are independent,
# large, standing sourcing hubs in their own right, real import volume
# regardless of whether it started as China-diversion or was always
# there. Always "expansion" when it fires, same posture as Latin
# America -- confirmation of a real, scaling import-driven business,
# not a distress read.
_ASIA_COUNTRIES = (
    "china", "vietnam", "india", "bangladesh", "cambodia", "thailand",
    "indonesia", "malaysia", "taiwan", "south korea", "japan",
)
_ASIA_RELEVANT_COMMODITIES = (
    "apparel", "auto_parts", "semiconductor_distributor", "ecommerce_seller", "electronics_manufacturer",
    # Tower cranes, excavators, and heavy construction machinery are
    # commonly imported from Japan/South Korea/China -- a real,
    # documented pattern for this population, not a guess.
    "crane_heavy_equipment",
)


def _asia_sourcing_note(payload: dict) -> tuple[str, str | None]:
    """A STANDING per-shipment check, same posture as
    _latin_america_sourcing_note. Deliberately skips any country
    already carrying a specific tariff classification for that same
    commodity (either the tariffed country itself, via
    _TARIFF_AFFECTED_COUNTRIES, or a named diversion beneficiary, via
    _TARIFF_DIVERSION_BENEFICIARY_COUNTRIES) -- a China-sourced apparel
    shipment stays a pure tariff-distress read from
    _tariff_trade_diversion_note, it never also gets tagged "expansion"
    here, which would directly contradict that finding. This only fires
    for the real Asian sourcing countries that currently have NO
    country-specific mechanism at all for that commodity -- e.g.
    Bangladesh/Cambodia/Vietnam/India for apparel, or Taiwan/South
    Korea/Japan for semiconductors.

    Returns ("", None) when the shipment's goods don't match a tracked
    category, or its country of origin isn't a major Asia sourcing hub,
    or that specific country is already covered by the tariff
    mechanism for this commodity."""
    description = _extract_goods_description(payload)
    country = _extract_country_of_origin(payload)
    if not description or not country:
        return "", None
    if not any(c in country for c in _ASIA_COUNTRIES):
        return "", None

    for commodity in _ASIA_RELEVANT_COMMODITIES:
        keywords = _COMMODITY_GOODS_KEYWORDS.get(commodity, ())
        if not keywords or not any(k in description for k in keywords):
            continue

        already_tariff_classified = any(
            c in country
            for c in (
                *_TARIFF_AFFECTED_COUNTRIES.get(commodity, ()),
                *_TARIFF_DIVERSION_BENEFICIARY_COUNTRIES.get(commodity, ()),
            )
        )
        if already_tariff_classified:
            continue

        return (
            f"ASIA SOURCING CONFIRMED: shipment's country of origin '{country}' is a major Asia "
            f"manufacturing/sourcing hub for {commodity} -- heavy, real import volume, a scaling "
            f"operation that needs capital to keep growing, not a distress read",
            "expansion",
        )
    return "", None


def _asia_import_signals() -> list[MacroSignal]:
    """The standing macro-level counterpart to _asia_sourcing_note --
    same posture as _latin_america_import_signals. Only generates a
    signal for electronics_manufacturer, the one commodity in
    _ASIA_RELEVANT_COMMODITIES with no existing standing nationwide
    signal of its own -- apparel/auto_parts/semiconductor_distributor/
    ecommerce_seller already get swept nationwide every cycle via
    _tariff_policy_signals (they're all in _TARIFF_AFFECTED_COUNTRIES),
    so a second always-on signal for them here would just double the
    same nationwide hunt rather than reaching anything new. The per-
    shipment check above still covers all five regardless."""
    return [
        MacroSignal(
            kind="asia_sourcing",
            description="Standing Asia import-volume signal for electronics_manufacturer",
            source_reference="asia_sourcing:electronics_manufacturer",
            commodity="electronics_manufacturer",
            signal_type="expansion",
        )
    ]


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
    # NGL processors/intermediate producers cluster tightly around the
    # same industrial corridors as the plants that buy from them
    # (Houston Ship Channel, Baton Rouge) -- same tight radius.
    "petrochemical_producer": 10.0,
    "port_congestion": 60.0,
    # Trucking companies aren't tied to one dock the way a mill or
    # elevator is -- a diesel-price shock affects fleets regionally, so
    # this needs the same wide radius as port congestion, not a tight
    # waterside-facility one.
    "logistics_fuel": 60.0,
    # Processors, wholesalers, retailers, and foodservice operators are
    # all everywhere, not clustered at a dock or plant -- same wide-
    # catchment logic as port_congestion/logistics_fuel. Mostly relevant
    # via the nationwide sweep (a cattle/hog/dairy price shock has no
    # single location), not a specific lock radius.
    "meat_processor": 50.0,
    "food_wholesaler": 50.0,
    "food_retailer": 50.0,
    "food_service": 50.0,
    # Ranches, feedlots, and dairy farms are genuinely rural and
    # dispersed -- even wider than the downstream food-chain tiers'
    # catchment, and mostly relevant via the nationwide sweep for the
    # same reason (a price shock has no single location).
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
    # Fertilizer terminals/distributors cluster at river import gateways
    # and ag-retail hubs much like a grain elevator does -- same radius.
    "fertilizer": 25.0,
    # Fulfillment centers/3PLs serve a wide regional catchment, not one
    # dock -- same wide-catchment logic as port_congestion/logistics_fuel.
    "ecommerce_logistics": 60.0,
    # Electronics distributors/manufacturers are dispersed nationally
    # (Silicon Valley/Austin/Phoenix concentration exists, but these
    # commodities flow almost entirely through the nationwide sweep,
    # same reasoning as aerospace_defense -- no CME price proxy, no
    # single corridor the way a river commodity has).
    "semiconductor_distributor": 50.0,
    "electronics_manufacturer": 50.0,
    # Furniture/wood importers cluster at the same major container
    # ports as ecommerce_logistics (real weight added to LA/Long Beach
    # and Savannah in usace.py -- furniture is a dominant container-
    # import category at both) -- wide catchment for the nationwide-
    # sweep fallback, same as every other no-single-corridor commodity.
    "furniture_wood": 50.0,
    # Apparel/footwear and auto parts are both consumer-goods-style
    # import categories with no single corridor the way a river
    # commodity has -- same wide-catchment logic.
    "apparel": 50.0,
    "auto_parts": 50.0,
    # Construction/renovation contractors are everywhere, driven almost
    # entirely by the HigherGov standing channel -- nationwide sweep,
    # same as aerospace_defense.
    "construction_contractor": 50.0,
    # Same nationwide-sweep-only logic -- commercial GCs and crane/
    # heavy-equipment companies are dispersed nationwide, no single
    # corridor to anchor to.
    "commercial_construction": 50.0,
    "crane_heavy_equipment": 50.0,
    # Poultry farms are rural/dispersed like livestock_producer -- same
    # nationwide-sweep-only, wide-catchment logic.
    "poultry_producer": 60.0,
    "chemical_products_manufacturer": 50.0,
    # Solar/lumber/EV-charging/rare-earth all nationwide-sweep-only --
    # none anchor to a single river/port corridor the way grain or
    # petrochemical does.
    "solar_installer": 50.0,
    "solar_manufacturer": 50.0,
    "lumber_wholesaler": 50.0,
    "lumber_producer": 50.0,
    "ev_charging_installer": 50.0,
    "rare_earth_importer": 50.0,
    # Same wide-catchment logic -- e-commerce sellers aren't tied to
    # one dock any more than their 3PLs are.
    "ecommerce_seller": 60.0,
    # Same wide-catchment logic as the food-chain tiers -- wholesalers/
    # retailers in these categories are dispersed nationally, not
    # clustered at a dock or plant.
    "produce_wholesaler": 50.0,
    "produce_retailer": 50.0,
    "liquor_distributor": 50.0,
    "liquor_retailer": 50.0,
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
    "steel_coal": ("steel", "iron", "ore", "coal", "metal", "foundry", "mill", "smelt", "aluminum", "copper"),
    "petrochemical": ("petrochemical", "chemical", "refinery", "petroleum", "plastics", "oil", "natural gas", "lng"),
    "petrochemical_producer": ("petrochemical", "chemical", "refinery", "petroleum", "plastics", "oil", "natural gas", "lng", "ngl processing"),
    # "container"/"shipping"/"cargo" added specifically so a Red Sea/
    # Suez/Panama Canal chokepoint headline -- which talks about ships
    # and canals, not warehouses -- still classifies as port_congestion
    # via _classify_commodity_from_text instead of matching nothing.
    "port_congestion": ("warehouse", "distribution", "logistics", "fulfillment", "freight", "terminal", "container", "shipping", "cargo"),
    "logistics_fuel": ("trucking", "logistics", "freight", "warehouse", "distribution", "transportation", "fleet"),
    # Four distinct downstream tiers of the same livestock/dairy
    # complex -- processor, wholesaler, retailer, foodservice -- split
    # out of what used to be one undifferentiated "food_cost" bucket.
    # This is the Argentina-beef chain made concrete: a price move
    # doesn't just affect "restaurants," it cascades through meat
    # processing plants, wholesalers, supermarkets, AND restaurants,
    # each a distinct business type worth its own targeted search
    # rather than one blended Apollo/Cobalt query that can't tell a
    # distributor from a diner.
    # "slaughterhouse" moved here from livestock_producer below -- a
    # real classification bug, caught by actually tracing the
    # economics: a slaughterhouse BUYS live cattle and SELLS meat, the
    # same cost-input relationship as any other processor, not a
    # seller of cattle. Under livestock_producer's trade_side
    # ("producer"), a cattle price spike would have been wrongly
    # framed as a windfall for a slaughterhouse, when it's actually a
    # cost squeeze -- the same mirror-image mistake the whole
    # producer/consumer split exists to prevent elsewhere.
    "meat_processor": ("meat processing", "poultry processing", "dairy processing", "food manufacturing", "meat packing", "slaughterhouse"),
    "food_wholesaler": ("food distribution", "wholesale grocery", "foodservice distribution", "cold storage warehouse"),
    "food_retailer": ("supermarket", "grocery", "grocery store", "food retail"),
    "food_service": ("restaurant", "food service", "catering"),
    # "meat packing" removed too -- same economic entity as
    # "slaughterhouse" (a meatpacking plant buys live animals, it
    # doesn't sell them), already correctly present in meat_processor
    # above.
    "livestock_producer": ("cattle ranch", "feedlot", "dairy farm", "livestock", "ranch"),
    "aerospace_defense": ("aerospace", "defense contractor", "aircraft", "avionics", "missile", "satellite", "aviation", "naval", "shipbuilding", "ammunition", "weapons system", "tactical vehicle", "defense electronics", "military logistics"),
    "fertilizer": ("fertilizer", "potash", "ammonia", "agricultural chemical", "crop input", "agribusiness"),
    "ecommerce_logistics": ("fulfillment", "warehouse", "distribution", "logistics", "parcel", "last mile", "cold storage"),
    # "neon"/"wafer"/"fab" catch the Ukraine-neon and Taiwan/China chip
    # angles specifically -- this list doubles as the keyword vocabulary
    # _classify_commodity_from_text uses for a geopolitical headline, not
    # just Regrid land-use text, same dual-purpose reuse as every other
    # commodity here.
    "semiconductor_distributor": ("semiconductor", "electronics distribution", "chip distributor", "electronic component"),
    "electronics_manufacturer": ("semiconductor", "electronics manufacturing", "circuit board assembly", "chip fabrication", "wafer fab", "neon gas"),
    "furniture_wood": ("furniture", "woodworking", "cabinetry", "sawmill", "lumber", "wood products"),
    "apparel": ("apparel", "clothing", "garment", "footwear", "textile"),
    "auto_parts": ("auto parts", "automotive parts", "auto components", "vehicle parts distribution"),
    # Checked BEFORE construction_contractor's bare "construction"
    # keyword below -- _classify_commodity_from_text returns the FIRST
    # matching commodity in this dict's iteration order, and
    # "commercial construction"/"industrial construction" text would
    # always also contain the word "construction," so the specific
    # commercial keywords have to come first or they'd never be
    # reached; a residential/general headline with no "commercial"/
    # "industrial"/"design-build" language still correctly falls
    # through to construction_contractor below.
    "commercial_construction": ("commercial construction", "commercial general contractor", "industrial construction", "design-build", "construction management"),
    "crane_heavy_equipment": ("crane rental", "crane service", "heavy equipment rental", "construction equipment rental", "equipment leasing"),
    # Added "excavating"/"excavation"/"earthwork"/"site work"/
    # "demolition" -- a real, previously-missing population (grading/
    # earthwork/demolition contractors) that construction_contractor's
    # vocabulary never reached at all.
    "construction_contractor": ("construction", "general contractor", "renovation", "building contractor", "excavating", "excavation", "earthwork", "site work", "demolition"),
    "poultry_producer": ("poultry", "chicken", "broiler", "live poultry"),
    "chemical_products_manufacturer": ("detergent", "soap", "cleaning product", "coating", "adhesive"),
    "solar_installer": ("solar installation", "solar contractor", "photovoltaic installer"),
    "solar_manufacturer": ("solar panel manufacturing", "photovoltaic manufacturing", "solar cell production"),
    "lumber_wholesaler": ("lumber distribution", "lumber yard", "building materials wholesale"),
    "lumber_producer": ("sawmill", "lumber mill", "timber processing"),
    # "rare earth" alone added -- without it, a real headline like
    # "China restricts rare earth exports" would never match (the
    # longer phrases alone are too narrow for _classify_commodity_
    # from_text, which needs the keyword to appear as written).
    "rare_earth_importer": ("rare earth", "rare earth processing", "rare earth refining", "magnet manufacturing"),
    "ev_charging_installer": ("ev charging installation", "electrical contractor", "charging infrastructure"),
    "ecommerce_seller": ("e-commerce", "online retail", "direct-to-consumer", "private label brand"),
    "produce_wholesaler": ("produce distribution", "fruit and vegetable wholesale", "fresh produce", "cold storage"),
    "produce_retailer": ("produce market", "farmers market", "fruit stand", "grocery"),
    "liquor_distributor": ("liquor distribution", "beverage distributor", "spirits importer"),
    "liquor_retailer": ("liquor store", "wine shop", "beverage retail"),
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
    # See MacroSignal.dynamic_apollo_keywords/dynamic_cobalt_term -- the
    # Apollo/Cobalt hunts use these directly instead of looking up the
    # static per-commodity dicts when a target carries them.
    dynamic_apollo_keywords: list[str] | None = None
    dynamic_cobalt_term: str | None = None


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

# Livestock_producer was sweeping the generic book-of-business cities
# above -- CA/FL/NY/TX/NJ/IL -- which is a real mismatch: cattle
# ranchers and dairy farms don't cluster in Los Angeles or Newark,
# they cluster in cattle-feedlot country (Texas Panhandle, Kansas,
# Nebraska) and real dairy regions (Wisconsin, Idaho, upstate New
# York, California's Central Valley). "These make money the same" as
# anyone else here -- a real, profitable population deserves a sweep
# that reflects where it actually is, not a generic fallback built
# for a different kind of business entirely.
_COMMODITY_SPECIFIC_SWEEP_LOCATIONS: dict[str, list[dict]] = {
    "livestock_producer": [
        {"apollo_location": "Amarillo, Texas, US", "state": "TX"},
        {"apollo_location": "Garden City, Kansas, US", "state": "KS"},
        {"apollo_location": "Omaha, Nebraska, US", "state": "NE"},
        {"apollo_location": "Madison, Wisconsin, US", "state": "WI"},
        {"apollo_location": "Twin Falls, Idaho, US", "state": "ID"},
        {"apollo_location": "Fresno, California, US", "state": "CA"},
    ],
}


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
                    dynamic_apollo_keywords=signal.dynamic_apollo_keywords, dynamic_cobalt_term=signal.dynamic_cobalt_term,
                )
            )
            continue

        # No single place -- sweep the nationwide locations instead of
        # skipping this signal entirely. A commodity with its own real
        # regional geography (see _COMMODITY_SPECIFIC_SWEEP_LOCATIONS)
        # sweeps THAT instead of the generic book-of-business cities.
        sweep_locations = _COMMODITY_SPECIFIC_SWEEP_LOCATIONS.get(signal.commodity, _NATIONWIDE_SWEEP_LOCATIONS)
        for place in sweep_locations:
            targets.append(
                GeoTarget(
                    lat=None, lon=None,  # no real geography behind a nationwide sweep -- see GeoTarget docstring
                    commodity=signal.commodity, label=f"{signal.description} (nationwide sweep: {place['apollo_location']})",
                    radius_mi=radius, weight=signal.weight,
                    location_hint=place["apollo_location"], state=place["state"], signal_type=signal.signal_type,
                    macro_corroborated=signal.macro_corroborated,
                    dynamic_apollo_keywords=signal.dynamic_apollo_keywords, dynamic_cobalt_term=signal.dynamic_cobalt_term,
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
    "petrochemical_producer": ["NGL processor", "natural gas liquids", "chemical producer", "resin producer", "intermediate chemical manufacturer"],
    "port_congestion": ["import", "distribution center", "freight forwarding", "warehousing and storage", "wholesale trade"],
    "logistics_fuel": ["trucking company", "freight carrier", "logistics company", "transportation company", "trucking fleet"],
    "meat_processor": ["meat processing company", "poultry processor", "dairy processor", "food manufacturer", "meat packing plant", "slaughterhouse"],
    "food_wholesaler": ["food distributor", "wholesale grocer", "foodservice distributor", "meat wholesaler"],
    "food_retailer": ["supermarket chain", "grocery store", "grocery retailer"],
    "food_service": ["restaurant", "food service company", "catering company", "restaurant chain"],
    "livestock_producer": ["cattle ranch", "feedlot operator", "dairy farm", "livestock producer"],
    "aerospace_defense": ["aerospace manufacturer", "defense contractor", "aircraft parts supplier", "avionics company", "aerospace supplier", "naval shipbuilder", "ammunition manufacturer", "tactical vehicle manufacturer", "defense electronics company", "military logistics contractor"],
    "fertilizer": ["fertilizer distributor", "agricultural chemical company", "crop input supplier", "farm supply", "agribusiness"],
    "ecommerce_logistics": ["fulfillment center", "third-party logistics", "last-mile delivery", "e-commerce logistics", "parcel carrier", "warehousing and storage"],
    "semiconductor_distributor": ["electronics distributor", "semiconductor distributor", "electronic component distributor"],
    "electronics_manufacturer": ["electronics manufacturer", "contract electronics manufacturer", "circuit board assembly", "electronics manufacturing services"],
    # Deliberately covers BOTH importers/distributors and domestic
    # manufacturers under one keyword list -- the tariff logic needs
    # to find all three populations (tariffed-country importer,
    # diversion-beneficiary importer, domestic producer), and Apollo's
    # org search can't pre-filter by country of origin the way Import
    # Genius's own records can (see _tariff_trade_diversion_note).
    "furniture_wood": ["furniture manufacturer", "furniture importer", "furniture wholesaler", "wood products manufacturer", "cabinetry manufacturer", "furniture store", "furniture retailer"],
    # "clothing store"/"footwear retailer" added -- the import/
    # wholesale/manufacturer keywords alone never actually reached
    # retail stores, even though the commodity was meant to cover the
    # whole chain the same way furniture_wood claims to (same gap
    # fixed there below).
    "apparel": ["apparel importer", "clothing wholesaler", "footwear distributor", "garment manufacturer", "textile importer", "clothing store", "footwear retailer"],
    "auto_parts": ["auto parts distributor", "automotive parts supplier", "auto parts wholesaler", "automotive components importer"],
    "construction_contractor": ["general contractor", "construction company", "renovation contractor", "building contractor", "excavating contractor", "excavation company", "earthwork contractor", "demolition contractor"],
    "commercial_construction": ["commercial general contractor", "commercial construction company", "industrial construction firm", "design-build contractor", "construction management firm"],
    "crane_heavy_equipment": ["crane rental company", "crane service provider", "heavy equipment rental company", "construction equipment dealer", "equipment leasing company"],
    "solar_installer": ["solar installation company", "solar contractor", "photovoltaic installer", "solar energy contractor"],
    "solar_manufacturer": ["solar panel manufacturer", "photovoltaic manufacturer", "solar cell manufacturer", "solar module producer"],
    "lumber_wholesaler": ["lumber wholesaler", "lumber importer", "building materials distributor", "lumber yard"],
    "lumber_producer": ["sawmill operator", "lumber mill", "timber producer", "softwood lumber manufacturer"],
    "ev_charging_installer": ["ev charging installer", "evse contractor", "electrical contractor", "charging infrastructure company"],
    "rare_earth_importer": ["rare earth importer", "rare earth materials company", "magnet manufacturer", "rare earth processor"],
    # The real "in" -- deliberately NOT reusing ecommerce_logistics'
    # keywords (fulfillment center/3PL/parcel carrier), which only
    # ever reach the service providers. This is the actual lucrative
    # MCA population: the brand itself, found by its own identity, not
    # its vendor's.
    "ecommerce_seller": ["e-commerce brand", "direct-to-consumer brand", "online retailer", "private label brand", "Amazon seller", "Shopify brand"],
    "poultry_producer": ["poultry farm", "chicken producer", "broiler operation", "poultry hatchery"],
    "chemical_products_manufacturer": ["detergent manufacturer", "soap manufacturer", "cleaning products company", "coatings manufacturer", "adhesives manufacturer"],
    "produce_wholesaler": ["produce wholesaler", "fresh produce distributor", "fruit and vegetable importer"],
    "produce_retailer": ["produce market", "grocery store", "specialty food market"],
    "liquor_distributor": ["liquor distributor", "beverage distributor", "spirits importer"],
    "liquor_retailer": ["liquor store", "wine and spirits retailer", "beverage retailer"],
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
    # Keyed by (location, commodity), NOT location alone -- the
    # food-chain tiering above means up to 5 distinct commodities can
    # now share the same nationwide-sweep city in one sweep
    # (meat_processor/food_wholesaler/food_retailer/food_service/
    # livestock_producer all sweep the same 6 metros). A location-only
    # key would silently search for the FIRST commodity at that city
    # and drop the other 4 entirely -- that's one real paid call per
    # distinct (place, industry) combination, not per place.
    seen: set[tuple[str, str]] = set()

    for target in targets:
        if not target.location_hint:
            continue
        key = (target.location_hint, target.commodity)
        if key in seen:
            continue
        seen.add(key)

        keywords = target.dynamic_apollo_keywords or _COMMODITY_APOLLO_KEYWORDS.get(target.commodity)
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
    "petrochemical_producer": "chemical producer",
    "port_congestion": "distribution",
    "logistics_fuel": "trucking",
    "meat_processor": "meat processing",
    "food_wholesaler": "food distributor",
    "food_retailer": "grocery",
    "food_service": "restaurant",
    "livestock_producer": "livestock",
    "aerospace_defense": "aerospace",
    "fertilizer": "fertilizer",
    "ecommerce_logistics": "fulfillment",
    "semiconductor_distributor": "electronics distributor",
    "electronics_manufacturer": "electronics manufacturer",
    "furniture_wood": "furniture manufacturer",
    "apparel": "clothing wholesaler",
    "auto_parts": "auto parts distributor",
    "construction_contractor": "general contractor",
    "commercial_construction": "commercial general contractor",
    "crane_heavy_equipment": "heavy equipment rental",
    "poultry_producer": "poultry farm",
    "chemical_products_manufacturer": "chemical products manufacturer",
    "solar_installer": "solar contractor",
    "solar_manufacturer": "solar panel manufacturer",
    "lumber_wholesaler": "lumber wholesaler",
    "lumber_producer": "sawmill",
    "ev_charging_installer": "electrical contractor",
    "rare_earth_importer": "rare earth importer",
    "ecommerce_seller": "online retailer",
    "produce_wholesaler": "produce distributor",
    "produce_retailer": "grocery",
    "liquor_distributor": "beverage distributor",
    "liquor_retailer": "liquor store",
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
    # Keyed by (state, commodity), same fix and same reason as the
    # Apollo hunt's dedup above -- up to 5 food-chain commodities can
    # now share one nationwide-sweep state in a single sweep.
    seen: set[tuple[str, str]] = set()

    for target in targets:
        if not target.state:
            continue
        key = (target.state, target.commodity)
        if key in seen:
            continue
        seen.add(key)

        search_term = target.dynamic_cobalt_term or _COMMODITY_COBALT_SEARCH_TERMS.get(target.commodity)
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
    # ZC (corn) and ZM (soybean meal) are poultry's two real feed-cost
    # inputs -- adding "poultry_producer" as a THIRD tag on these two
    # (not ZW wheat or ZL soybean oil, neither a real poultry feed
    # input) gives corn/soy producers their existing expansion/distress
    # framing while ALSO tagging poultry farms as consumer-side: a
    # grain price spike is a feed-cost squeeze for them, not a
    # windfall, the same mirror-image logic as petrochemical plants
    # buying NG. This is poultry's ONLY price-driven trigger -- unlike
    # cattle/hogs/dairy, there's no actively-traded CME broiler/poultry
    # futures contract to tag directly (see SYMBOL_COMMODITIES'
    # cattle/hog/dairy comment below for that contrast).
    "ZC.c.0": ["grain", "poultry_producer"], "ZS.c.0": ["grain"], "ZW.c.0": ["grain"],
    "ZM.c.0": ["grain", "poultry_producer"], "ZL.c.0": ["grain"],
    # "commercial_construction" added as a second tag -- commercial/
    # industrial GCs buy structural steel directly and in real volume
    # (high-rises, industrial buildings), a direct cost-input link, not
    # the more indirect one crane_heavy_equipment has (see
    # _COMMODITY_TRADE_SIDE's comment on why that one is deliberately
    # NOT tagged here). Residential/general construction_contractor is
    # mostly lumber-framed, not steel-framed, so it's deliberately left
    # off this symbol too -- the real link there is lumber, which has
    # no CME-listed futures symbol wired into this project.
    "HRC.c.0": ["steel_coal", "commercial_construction"],
    "CL.c.0": ["logistics_fuel"], "HO.c.0": ["logistics_fuel"],
    # US petrochemical cracking overwhelmingly runs on natural-gas-
    # liquids feedstock (ethane), not crude -- so NG, not CL/HO, is the
    # real petrochemical feedstock signal. Same dual-tag pattern as
    # livestock: one price move, two populations with opposite stories
    # -- plants that BUY NG as feedstock (squeezed by a spike) and NGL
    # processors/intermediate producers who SELL it (windfall). A
    # THIRD tag added here -- chemical_products_manufacturer -- one
    # tier further downstream: detergent/soap/coatings/adhesives
    # manufacturers who buy RESINS (not raw NG) as their input, the
    # same real trickle-down the Argentina-beef chain has at the food
    # retailer/foodservice tiers, just one commodity over.
    "NG.c.0": ["petrochemical", "petrochemical_producer", "chemical_products_manufacturer"],
    # Each livestock/dairy symbol now tags FIVE populations off one
    # price move, not two -- the producer side, plus all four
    # downstream tiers of the Argentina-beef chain (processor,
    # wholesaler, retailer, foodservice), each independently classified
    # by _COMMODITY_TRADE_SIDE and each getting its own distinct
    # Apollo/Cobalt hunt (see _COMMODITY_APOLLO_KEYWORDS) instead of one
    # blended "food_cost" search that couldn't tell a distributor from
    # a diner.
    "LE.c.0": ["livestock_producer", "meat_processor", "food_wholesaler", "food_retailer", "food_service"],
    "GF.c.0": ["livestock_producer", "meat_processor", "food_wholesaler", "food_retailer", "food_service"],
    "HE.c.0": ["livestock_producer", "meat_processor", "food_wholesaler", "food_retailer", "food_service"],
    "DC.c.0": ["livestock_producer", "meat_processor", "food_wholesaler", "food_retailer", "food_service"],
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
    # The seller side of the petrochemical complex -- NGL processors/
    # intermediate chemical producers who SELL what "petrochemical"
    # above buys as feedstock. A price UP (NG shock, or a geopolitical
    # oil/gas supply-at-risk headline) is a windfall for them, the
    # mirror image of the squeeze it is for the plants that buy it.
    "petrochemical_producer": "producer",
    "logistics_fuel": "consumer",
    # All four downstream food-chain tiers buy this commodity as an
    # input, same mirror-image logic as petrochemical/logistics_fuel.
    "meat_processor": "consumer",
    "food_wholesaler": "consumer",
    "food_retailer": "consumer",
    "food_service": "consumer",
    # The seller side of the livestock/dairy complex -- ranches,
    # feedlots, dairy farms, slaughterhouses/meat packers. A price DOWN
    # (e.g. cheap imports flooding the market) squeezes THEM even
    # though it's relief for the four downstream tiers above.
    "livestock_producer": "producer",
    # US ag buyers (farms, ag retailers, fertilizer distributors) buy
    # potash/fertilizer as an input -- same mirror-image logic as
    # petrochemical. No CME-listed futures contract for potash, but
    # this still drives the geopolitical disruption classifier below.
    "fertilizer": "consumer",
    # Both downstream semiconductor/electronics tiers buy chips/
    # materials as an input, same mirror-image logic as every other
    # consumer-side commodity.
    "semiconductor_distributor": "consumer",
    "electronics_manufacturer": "consumer",
    # Previously missing entirely -- auto parts distributors/wholesalers
    # BUY components as their core input, same consumer-side mirror logic
    # as every other import-driven commodity here. Without this, a
    # geopolitical "auto parts supply at risk" headline could classify
    # the commodity but never produce a distress/expansion signal_type
    # off it, since _signal_type_for_price_direction had nothing to look
    # up.
    "auto_parts": "consumer",
    # Poultry farms buy corn/soybean meal as feed -- a grain price
    # spike is a cost squeeze, not the windfall it is for grain's own
    # producer side (see SYMBOL_COMMODITIES' ZC/ZM comment).
    "poultry_producer": "consumer",
    # Detergent/soap/coatings/adhesives manufacturers buy RESINS as
    # input, one tier further downstream from the plants petrochemical
    # already covers -- same mirror-image logic, different rung of the
    # same ladder.
    "chemical_products_manufacturer": "consumer",
    # Solar installers buy panels as their core input -- a China
    # tariff on panels/cells is a cost squeeze for them.
    "solar_installer": "consumer",
    # Domestic solar manufacturers SELL panels -- the same China
    # tariff that squeezes installers is a competitive windfall for
    # them, real deal sizes here (confirmed up to $5M+) and a real,
    # recurring capital need: tariff-driven input/price swings, A/R
    # timing against large commercial/utility customers, and genuine
    # expansion capital to scale capacity -- "large company" doesn't
    # disqualify MCA fit the way it was first assumed to.
    "solar_manufacturer": "producer",
    # Lumber importers/wholesalers buy Canadian softwood as their
    # core input -- the tariff (a real, decades-long US-Canada
    # softwood lumber countervailing/anti-dumping dispute, distinct
    # from the general steel/aluminum Section 232 action) is a direct
    # cost squeeze.
    "lumber_wholesaler": "consumer",
    # US sawmills/domestic lumber producers are protected by that same
    # tariff -- the real expansion side, same mirror-image logic as
    # steel_coal's domestic-producer framing.
    "lumber_producer": "producer",
    # Rare earth importers/processors buy raw rare earth elements as
    # input -- China's export controls are a direct scarcity/cost
    # squeeze, same mechanism as every other consumer-side commodity
    # facing a geopolitical supply-at-risk headline.
    "rare_earth_importer": "consumer",
    # Construction/commercial-construction contractors buy steel/
    # lumber/concrete as direct cost inputs -- a price-up headline (a
    # steel-price shock, a geopolitical supply-at-risk story) is a cost
    # squeeze for them, the mirror image of steel_coal/lumber_producer's
    # own producer-side framing.
    "construction_contractor": "consumer",
    "commercial_construction": "consumer",
    # Crane/heavy-equipment companies' machinery is steel-intensive, so
    # the same mirror-image logic applies, even though (deliberately)
    # NOT tagged to the HRC steel-futures symbol in SYMBOL_COMMODITIES
    # below -- that link (equipment replacement cost rising with steel
    # prices) is real but more indirect than a contractor buying
    # structural steel directly, so it's left to fire only off an
    # actual crane/heavy-equipment-specific headline, not a generic
    # steel price tick.
    "crane_heavy_equipment": "consumer",
}


def _signal_type_for_price_direction(commodity: str, price_up: bool) -> str | None:
    """Shared producer/consumer x direction classification -- used by
    both an actual price move (_cme_price_shock_signals) and a
    geopolitical supply-disruption headline
    (_gdelt_geopolitical_disruption_signals), which has no real price
    number but implies the same direction (a war/sanctions/export-ban
    on a commodity that concentrated is supply AT RISK, i.e. implied
    price UP) -- same mirror-image logic either way: a price UP is a
    revenue windfall for a producer (expansion) and a cost squeeze for
    a consumer (distress), and the reverse for a price DOWN. Returns
    None for an untagged/unclassified commodity -- never guessed."""
    trade_side = _COMMODITY_TRADE_SIDE.get(commodity)
    if trade_side == "producer":
        return "expansion" if price_up else "distress"
    if trade_side == "consumer":
        return "distress" if price_up else "expansion"
    return None


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

            signal_type = _signal_type_for_price_direction(commodity, price_up)
            trade_side = _COMMODITY_TRADE_SIDE.get(commodity)
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


# Below this, an LLM classification is treated as "not confident
# enough to spend a real paid Apollo/Cobalt hunt on" and discarded --
# a bad/uncertain classification is worse than none here.
_LLM_CLASSIFICATION_CONFIDENCE_FLOOR = 0.6
# Real cost control: unlike GDELT (free), each of these is a paid
# Anthropic API call -- cap how many unclassified headlines get sent
# per sweep regardless of how many the broad query returns.
_LLM_MAX_ARTICLES_PER_SWEEP = 10


def _llm_classify_news(title: str) -> dict | None:
    """The actual answer to "find a correlation, not just match against
    a list I already coded." _classify_commodity_from_text can only
    ever recognize the handful of industries already hand-wired into
    this file. An LLM call can recognize a genuinely NOVEL industry a
    headline implies -- "Widget Corp signs supply deal" as being about
    semiconductor components, say -- and reason about financing
    direction the same way the producer/consumer trade-side logic
    does, without that industry ever having been pre-coded here.

    This calls a real, confirmed API (api.anthropic.com/v1/messages) --
    not a guessed endpoint the way several other adapters in this
    project still are. Returns None (not a guess) when no key is
    configured, the HTTP call fails, the response isn't parseable
    JSON, or the model's own reported confidence is below
    _LLM_CLASSIFICATION_CONFIDENCE_FLOOR.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        return None

    prompt = (
        "Classify this news headline for a business lead-generation system. Respond with ONLY valid JSON, "
        "no markdown formatting, no explanation.\n\n"
        f'Headline: "{title}"\n\n'
        "If this headline is clearly about a specific, real industry or business sector, respond with:\n"
        '{"industry": "<short specific label, e.g. \\"lumber\\", \\"semiconductor manufacturing\\", '
        '\\"coffee roasting\\">", '
        '"signal_type": "expansion"|"distress"|"neutral", '
        '"apollo_keywords": ["<3 to 5 B2B company-search keywords for that exact industry>"], '
        '"cobalt_term": "<one search term for a Secretary of State business-name search>", '
        '"confidence": <0.0 to 1.0>}\n\n'
        'If the headline is generic financial/political news with no identifiable industry, respond with: '
        '{"industry": null}'
    )

    try:
        with httpx.Client(timeout=20) as client:
            response = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": settings.anthropic_model,
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            text = response.json()["content"][0]["text"].strip()
    except Exception:
        return None

    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not parsed.get("industry") or parsed.get("confidence", 0) < _LLM_CLASSIFICATION_CONFIDENCE_FLOOR:
        return None
    return parsed


def _gdelt_llm_discovery_signals(db: Session) -> list[MacroSignal]:
    """Real open-ended industry discovery: runs the same broad national
    query as _gdelt_broad_disruption_signals, but for every headline
    the FIXED keyword classifier (_classify_commodity_from_text)
    couldn't place -- never re-spending an LLM call rediscovering an
    industry already handled by the free keyword path. Capped at
    _LLM_MAX_ARTICLES_PER_SWEEP since each check is a real paid
    Anthropic API call, unlike everything else in this file.

    Always routed through the nationwide sweep (no location set) --
    see _geo_targets_from_signals -- since a newly-discovered industry
    was never anchored to one of the known MONITORED_LOCKS places. The
    hunt keywords/term travel WITH the signal (dynamic_apollo_keywords/
    dynamic_cobalt_term) rather than being looked up from the static
    per-commodity dicts, since a brand-new industry isn't in them.

    No API key configured -> returns [] immediately, no GDELT call
    even attempted, same "no key -> no-op" posture as every other
    adapter in this project.
    """
    if not get_settings().anthropic_api_key:
        return []

    adapter = GDELTAdapter()
    try:
        records = adapter.search(_BROAD_DISRUPTION_QUERY, maxrecords=30)
    except Exception:
        return []

    signals: list[MacroSignal] = []
    checked = 0
    for record in records:
        if _classify_commodity_from_text(record.title):
            continue  # already has a known match -- don't spend an LLM call rediscovering it
        if checked >= _LLM_MAX_ARTICLES_PER_SWEEP:
            break
        checked += 1

        result = _llm_classify_news(record.title)
        if not result:
            continue

        event = TelemetryEvent(
            source=TelemetrySource.GDELT,
            title=record.title,
            payload={**record.payload, "llm_discovered_industry": result["industry"], "llm_confidence": result["confidence"]},
        )
        db.add(event)
        db.flush()

        signal_type = result.get("signal_type")
        signals.append(
            MacroSignal(
                kind="llm_discovered_industry",
                description=(
                    f"LLM-discovered industry '{result['industry']}' (confidence {result['confidence']:.0%}, "
                    f"not a verified tag): {record.title}"
                ),
                source_reference=f"telemetry_event:{event.id} [gdelt] {record.title}",
                commodity=result["industry"],
                signal_type=signal_type if signal_type != "neutral" else None,
                dynamic_apollo_keywords=result.get("apollo_keywords"),
                dynamic_cobalt_term=result.get("cobalt_term"),
            )
        )

    db.commit()
    return signals


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


# REMOVED the calendar-based seasonal trigger -- e-commerce/fulfillment
# capital need isn't actually driven by the date, it's driven by real
# physical disruption at the ports these importers depend on: tariffs,
# sea traffic, and container hold-ups. ecommerce_logistics is now a
# weighted commodity directly on the 4 coastal port_congestion locks
# in usace.py (same mechanism as fertilizer's river-corridor weight),
# so a real AIS-confirmed queue at LA/Long Beach, Houston, NY/NJ, or
# Miami emits an ecommerce_logistics signal anchored to that port's
# REAL coordinates -- not a nationwide guess -- which is what actually
# lets Import Genius/Apollo/Cobalt/GDELT/GFW all converge on the same
# physical place, same invisible-thread logic as the Argentina-beef
# chain, just triggered by a port instead of a price.


# Geopolitical supply-disruption language, generalized across EVERY
# commodity that has a real foreign-concentration story, not just
# fertilizer -- this is the Argentina-beef chain's logic applied to
# war/sanctions/export-ban coverage: a conflict or export ban on a
# commodity concentrated abroad means its GLOBAL supply is at risk,
# which is the same real-world direction as a price move UP (scarcity)
# even though there's no CME-listed futures contract to read an actual
# number from for most of these (potash, neon/semiconductor materials,
# Ukrainian steel/grain). Rather than hardcoding one direction per
# commodity the way the old fertilizer-only version did, this runs the
# SAME producer/consumer x direction classification a real price shock
# gets (see _signal_type_for_price_direction) -- a war that threatens
# Ukraine's wheat exports is RELIEF/expansion for US grain producers
# (they capture the price windfall), while the same war threatening
# Russia/Belarus potash exports is DISTRESS for US ag buyers who
# depend on it. One mechanism, correctly opposite outcomes, same as
# the beef producer/consumer split.
#
# Country/hotspot list is deliberately broad (not fertilizer-specific
# anymore): Russia/Belarus/Ukraine (grain, steel, potash), Iran (urea/
# ammonia), Taiwan/China (semiconductors -- Taiwan fabs the overwhelming
# majority of advanced chips; Ukraine separately supplies a large share
# of the neon gas used in chip lithography), the Red Sea/Suez/Panama
# Canal chokepoints (general shipping/container disruption, classified
# to port_congestion/ecommerce_logistics via the "container"/
# "shipping"/"cargo" keywords added to that commodity's land-use list),
# and the Middle East/OPEC+/Russia for oil/natural gas -- the real
# petrochemical-feedstock angle a war or sanctions headline implies
# (supply at risk = price pressure up), same mechanism as every other
# commodity here.
_GEOPOLITICAL_SUPPLY_DISRUPTION_QUERY = (
    '(war OR sanctions OR "export ban" OR "export controls" OR blockade OR conflict OR OPEC) AND '
    '(grain OR wheat OR fertilizer OR potash OR ammonia OR steel OR semiconductor OR chip OR '
    '"neon gas" OR wafer OR container OR shipping OR oil OR "natural gas" OR LNG OR petrochemical OR '
    '"rare earth") AND '
    '(Russia OR Ukraine OR Belarus OR Iran OR Taiwan OR China OR "Red Sea" OR Suez OR "Panama Canal" OR "Middle East")'
)

# A geopolitical headline about oil/gas supply risk should produce the
# SAME dual-population split a real NG price shock does (see
# SYMBOL_COMMODITIES' NG.c.0 entry) -- but _classify_commodity_from_text
# only ever returns its FIRST keyword match, so it can't emit both tags
# from one headline on its own. This is the targeted fix: after the
# primary commodity is classified, also emit a signal for each listed
# sibling tag, each independently run through the same trade-side
# classification -- not a general rewrite of _classify_commodity_from_
# text (which several other callers rely on returning a single
# commodity), just this one dual-population case handled explicitly.
_GEOPOLITICAL_COMMODITY_SIBLING_TAGS: dict[str, list[str]] = {
    "petrochemical": ["petrochemical_producer", "chemical_products_manufacturer"],
}


def _gdelt_geopolitical_disruption_signals(db: Session, commodities: set[str] | None = None) -> list[MacroSignal]:
    """Nationwide, not per-place, same posture as _gdelt_shock_signals'
    national sweep -- none of these hotspots map to a single US
    location. One free GDELT call for the whole sweep, then each hit
    is classified to a commodity by keyword
    (_classify_commodity_from_text, the same function HigherGov/the
    national sweep already use) and given a direction via
    _signal_type_for_price_direction -- no hardcoded direction per
    commodity anymore.

    Routes through the nationwide sweep downstream by default (no
    coordinates/location_hint/state set here) -- UNLESS this sweep's
    USACE read ALSO shows a disruption on a river/port corridor that
    actually carries that commodity's real tonnage (fertilizer on the
    Mississippi/Illinois + New Orleans GIWW, port_congestion/
    ecommerce_logistics at the coastal ports -- see usace.py). That
    corroboration is what _hot_commodities_this_sweep +
    _stage1_detect_signals' macro_corroborated flag turns a
    geopolitical headline into a river/port-scan-confirmed hunt, not a
    global guess -- "narrow it down to the river scan" in code.

    Every hit is written as its own TelemetryEvent, same real-stored-
    row invariant as every other active-hunt path in this module.
    """
    adapter = GDELTAdapter()
    try:
        records = adapter.search(_GEOPOLITICAL_SUPPLY_DISRUPTION_QUERY, maxrecords=20)
    except Exception:
        return []  # a single failed call here just means no geopolitical disruption signal this sweep

    signals: list[MacroSignal] = []
    for record in records:
        primary_commodity = _classify_commodity_from_text(record.title)
        if not primary_commodity:
            continue

        event = TelemetryEvent(
            source=TelemetrySource.GDELT,
            title=record.title,
            payload={**record.payload, "geopolitical_commodity": primary_commodity},
        )
        db.add(event)
        db.flush()

        for commodity in [primary_commodity] + _GEOPOLITICAL_COMMODITY_SIBLING_TAGS.get(primary_commodity, []):
            if commodities is not None and commodity not in commodities:
                continue
            signal_type = _signal_type_for_price_direction(commodity, price_up=True)
            signals.append(
                MacroSignal(
                    kind="geopolitical_supply_disruption",
                    description=(
                        f"GDELT geopolitical supply-disruption coverage classified {commodity} "
                        f"({signal_type or 'unclassified'}): {record.title}"
                    ),
                    source_reference=f"telemetry_event:{event.id} [gdelt] {record.title}",
                    commodity=commodity,
                    signal_type=signal_type,
                )
            )

    db.commit()
    return signals


# Avian flu/bird flu outbreaks are a real, recurring, well-documented
# supply-destruction event for poultry -- not a price move, a direct
# cull. Unlike a tariff (which genuinely can go either way depending
# on which side of it you're on), a disease outbreak culling flocks
# has an unambiguous real-world direction: always distress for the
# affected population, same confidence posture as HigherGov's always-
# "expansion" and the fertilizer geopolitical signal's always-
# "distress" before it was generalized. Deliberately NOT extended
# here to "which OTHER producers benefit from a competitor's culled
# flock" (the same dual-sided logic petrochemical gets) -- that would
# need a per-region breakdown this national query doesn't give, a
# real limit, not a decision that the upside doesn't exist.
_AVIAN_FLU_QUERY = '("avian flu" OR "bird flu" OR "H5N1" OR "poultry outbreak" OR "flock culled")'


def _gdelt_avian_flu_signals(db: Session) -> list[MacroSignal]:
    """Poultry's only disease-driven trigger, distinct from and
    additive to its feed-cost trigger (see SYMBOL_COMMODITIES' ZC/ZM
    tags) -- a grain price spike and a bird flu outbreak are two
    completely different real capital-need stories for the same
    population (cost squeeze vs. direct flock loss), so both run,
    neither replaces the other. One free national GDELT call per
    sweep, same posture as every other national-level query here."""
    adapter = GDELTAdapter()
    try:
        records = adapter.search(_AVIAN_FLU_QUERY, maxrecords=20)
    except Exception:
        return []  # a single failed call here just means no avian flu signal this sweep

    signals: list[MacroSignal] = []
    for record in records:
        event = TelemetryEvent(
            source=TelemetrySource.GDELT,
            title=record.title,
            payload={**record.payload, "geopolitical_commodity": "poultry_producer"},
        )
        db.add(event)
        db.flush()
        signals.append(
            MacroSignal(
                kind="poultry_disease_outbreak",
                description=f"GDELT avian flu/bird flu outbreak coverage: {record.title}",
                source_reference=f"telemetry_event:{event.id} [gdelt] {record.title}",
                commodity="poultry_producer",
                signal_type="distress",
            )
        )

    db.commit()
    return signals


# The population flagged as missing: a domestic PRODUCER squeezed by
# a COMPETITOR's tariff relief, not by their own cost structure.
# Argentina getting a beef tariff exemption doesn't change a US
# rancher's costs -- it changes what they can sell for, by flooding
# the market with a cheaper substitute. Economically that's the same
# real-world direction as a price move DOWN for that commodity, so
# this reuses _signal_type_for_price_direction(commodity, price_up=
# False) rather than inventing a new classification rule -- same
# mechanism as every other signal here, just fed the opposite
# direction, because tariff RELIEF is the mirror image of tariff
# IMPOSITION/supply disruption.
#
# A dedicated keyword check, NOT _classify_commodity_from_text --
# that classifier returns only its FIRST match (petrochemical before
# petrochemical_producer on an oil/gas headline, which would wrongly
# skip the producer side this function specifically needs) and has no
# beef/cattle keywords for livestock_producer at all. Both are real
# limits of the general classifier that don't matter for its other
# callers but would silently break this one, so this checks the
# PRODUCER-side commodities directly instead.
_TARIFF_RELIEF_PRODUCER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "grain": ("grain", "wheat", "corn", "soybean"),
    "steel_coal": ("steel", "aluminum", "coal"),
    "livestock_producer": ("cattle", "beef", "livestock", "dairy"),
    "petrochemical_producer": ("natural gas", "lng", "petrochemical"),
}

_TARIFF_RELIEF_QUERY = (
    '("tariff exemption" OR "tariff waiver" OR "duty-free" OR "quota increase" OR "tariff relief" OR "import quota") AND '
    '(grain OR wheat OR beef OR cattle OR steel OR aluminum OR "natural gas" OR petrochemical OR dairy)'
)


def _gdelt_tariff_relief_signals(db: Session, commodities: set[str] | None = None) -> list[MacroSignal]:
    """Deliberately restricted to the PRODUCER side only (grain,
    steel_coal, livestock_producer, petrochemical_producer) -- the
    downstream consumer-side relief this same news creates (cheaper
    beef is good for restaurants) is already covered by the existing
    Latin America standing signal and the general GDELT sweep, so
    this doesn't duplicate it.

    HONEST LIMIT, deliberately NOT solved here: meat_processor
    (slaughterhouses) sits in a genuinely ambiguous spot for this
    exact trigger -- real competitive pressure on their OWN finished-
    goods sales from the same cheap import, but also real input-cost
    relief if softer domestic cattle demand follows. Those two effects
    pull in opposite directions and this system has no way to know
    which dominates, so meat_processor gets NO signal from this
    function -- not a distress guess, not an expansion guess,
    genuinely no claim. Forcing one would be exactly the weak signal
    this was built to avoid.

    No real place to anchor to (a national tariff-policy headline, not
    a located disruption) -- routes through the nationwide sweep same
    as every other place-less signal, driving the generic Apollo/
    Cobalt hunt for ranches/mills/dairy farms/NGL producers, the exact
    population Import Genius can never show you (they're not
    importers -- that's the whole point)."""
    adapter = GDELTAdapter()
    try:
        records = adapter.search(_TARIFF_RELIEF_QUERY, maxrecords=20)
    except Exception:
        return []  # a single failed call here just means no tariff-relief signal this sweep

    signals: list[MacroSignal] = []
    for record in records:
        title_lower = record.title.lower()
        for commodity, keywords in _TARIFF_RELIEF_PRODUCER_KEYWORDS.items():
            if not any(k in title_lower for k in keywords):
                continue
            if commodities is not None and commodity not in commodities:
                continue
            signal_type = _signal_type_for_price_direction(commodity, price_up=False)
            event = TelemetryEvent(
                source=TelemetrySource.GDELT,
                title=record.title,
                payload={**record.payload, "geopolitical_commodity": commodity},
            )
            db.add(event)
            db.flush()
            signals.append(
                MacroSignal(
                    kind="tariff_relief_competitive_pressure",
                    description=(
                        f"GDELT tariff-relief/exemption coverage -- competitive pressure on domestic "
                        f"{commodity} producers from a cheaper competing import ({signal_type}): {record.title}"
                    ),
                    source_reference=f"telemetry_event:{event.id} [gdelt] {record.title}",
                    commodity=commodity,
                    signal_type=signal_type,
                )
            )
            break  # one commodity match per headline is enough

    db.commit()
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
    # Same physical event (explosion/fire/shutdown at the SAME Gulf
    # Coast plant) matters to both sides of the petrochemical complex
    # -- the buyer-side plant directly, the seller-side NGL/
    # intermediate producer via the competitor-opportunity read (see
    # _DUAL_SIDED_DISRUPTION_COMMODITIES) -- so this reuses the exact
    # same query, not a separate one.
    "petrochemical_producer": '("petrochemical plant" OR refinery OR "chemical plant" OR explosion OR fire OR leak OR shutdown)',
    "port_congestion": '(port OR terminal OR "shipping delay" OR congestion OR backlog OR strike)',
    "logistics_fuel": '(trucking OR "freight carrier" OR "fuel surcharge" OR "diesel price" OR layoffs OR bankruptcy)',
    # Added -- fertilizer and furniture_wood both carry real weight on
    # real locks/ports (Mississippi/Illinois + New Orleans for
    # fertilizer; LA/Long Beach + Savannah for furniture) but were
    # missing from this dict entirely, meaning the LOCAL per-place
    # GDELT query never ran for them despite having a real place to
    # anchor to -- a real gap, not a deliberate exclusion like
    # livestock_producer/food_service's.
    "fertilizer": '("fertilizer plant" OR "potash mine" OR "ammonia plant" OR shortage OR "export ban" OR shutdown)',
    "furniture_wood": '("furniture factory" OR sawmill OR "wood products" OR shutdown OR layoffs OR tariff OR "supply chain")',
    # Apparel now carries real port weight too (see usace.py) -- same
    # fix, same reasoning.
    "apparel": '("apparel factory" OR garment OR textile OR tariff OR "supply chain" OR shutdown OR layoffs)',
    # Previously missing despite auto_parts carrying real weight at the
    # Port of Baltimore (ro-ro auto trade) -- same gap class as
    # fertilizer/furniture_wood/apparel earlier: a commodity with a real
    # place to anchor to, but the local per-place GDELT query never ran
    # for it.
    "auto_parts": '("auto parts factory" OR "automotive supplier" OR tariff OR "supply chain" OR shutdown OR layoffs)',
    # ecommerce_seller now carries real port weight too (see usace.py)
    # -- added here at the same time, not after the fact, this time.
    "ecommerce_seller": '("e-commerce" OR "online retailer" OR "direct-to-consumer" OR tariff OR "supply chain" OR container OR "inventory shortage")',
    # The actual trigger for e-commerce/import-retail capital need --
    # tariffs and container hold-ups, not a date on the calendar. Runs
    # at the SAME 4 coastal ports as port_congestion (see usace.py's
    # commodity weight on those locks), so a tariff/container headline
    # near LA/Houston/Newark/Miami corroborates an independent AIS-
    # confirmed queue at that exact port, not a nationwide guess.
    "ecommerce_logistics": '("container" OR tariff OR "import tax" OR "supply chain" OR "port congestion" OR backlog OR "shipping delay")',
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
    so every OTHER commodity is left plausible there; the four food-
    chain tiers (meat_processor/food_wholesaler/food_retailer/
    food_service) and livestock_producer are deliberately excluded
    from place-based classification -- none of them are geographically
    concentrated the way a mill or elevator is (restaurants/wholesalers/
    retailers are everywhere; ranches/feedlots are the opposite,
    genuinely rural and specifically NOT in a major metro) -- all are
    better served by the nationwide sweep already built, not a
    "mentioned near this city" guess)."""
    index: dict[str, dict] = {}
    for lock in MONITORED_LOCKS:
        city = lock["apollo_location"].split(",")[0].strip().lower()
        if city not in index:
            index[city] = {"lat": lock["lat"], "lon": lock["lon"], "state": lock["state"], "commodities": set((lock.get("commodities") or {}).keys())}
    metro_plausible_commodities = set(_COMMODITY_LAND_USE_KEYWORDS.keys()) - {
        "meat_processor", "food_wholesaler", "food_retailer", "food_service", "livestock_producer",
        "produce_wholesaler", "produce_retailer", "liquor_distributor", "liquor_retailer",
    }
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

# See _usace_disruption_signals' docstring -- "doesn't matter who's
# the winner or loser, we capitalize both." Petrochemical is the
# proof case (Gulf Coast corridor, explosion/hurricane-prone); the
# same mechanism applies to any commodity added here.
_DUAL_SIDED_DISRUPTION_COMMODITIES = {"petrochemical", "petrochemical_producer"}


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
    re-deriving it from the lock name.

    DUAL-SIDED for commodities in _DUAL_SIDED_DISRUPTION_COMMODITIES --
    "doesn't matter who's the winner or loser, we capitalize both." A
    disrupted plant/lock doesn't just hurt the business it directly
    affects; every OTHER producer in that same corridor and commodity
    that's still operating has a real capacity/share-capture
    opportunity. Starting with petrochemical (the Gulf Coast corridor
    is explosion/hurricane-prone, and a disrupted plant's lost orders
    have to go somewhere) -- deliberately NOT applied to every
    commodity yet, extend the set the same way once you want the same
    logic for grain/steel_coal/other corridors."""
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
            if commodity in _DUAL_SIDED_DISRUPTION_COMMODITIES:
                signals.append(
                    MacroSignal(
                        kind="river_corridor_competitor_opportunity",
                        description=(
                            f"USACE {label} disruption ({weight:.0%} of this corridor's traffic) -- COMPETITOR "
                            f"OPPORTUNITY READ: a real share-capture opportunity for every OTHER {label} "
                            f"producer still operating in this same corridor, not the directly affected "
                            f"business itself: {event.title}"
                        ),
                        source_reference=f"telemetry_event:{event.id} [usace] {event.title}",
                        commodity=commodity,
                        coordinates=coordinates,
                        weight=weight,
                        location_hint=location_hint,
                        state=state,
                        signal_type="expansion",
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
    instead of treating every hit as equally uncertain.

    ALSO includes real open-ended industry discovery via an LLM
    classification pass over headlines the fixed keyword vocabulary
    couldn't place (_gdelt_llm_discovery_signals) -- a no-op with no
    ANTHROPIC_API_KEY configured, a real paid call (capped per sweep)
    once one is.

    ALSO includes a geopolitical macro-level channel
    (_gdelt_geopolitical_disruption_signals -- a war/sanctions/export-
    ban headline covering grain, fertilizer, steel, semiconductors, or
    a shipping chokepoint, classified by commodity and given a real
    producer/consumer direction, not a hardcoded one, treated the same
    priority as a CME price shock or a contract award). e-commerce/
    fulfillment has no dedicated calendar channel here -- it's driven
    by real port disruption instead, so it surfaces through
    usace_signals itself (ecommerce_logistics is now a weighted
    commodity on the 4 coastal port_congestion locks, see usace.py)."""
    cme_signals = _cme_price_shock_signals(db)
    national_gdelt_signals = _gdelt_shock_signals(db)
    contract_signals = _highergov_contract_signals(db)
    geopolitical_signals = _gdelt_geopolitical_disruption_signals(db)
    hot = _hot_commodities_this_sweep(cme_signals + national_gdelt_signals + contract_signals + geopolitical_signals)

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
        + geopolitical_signals
        + local_signals
        + broad_signals
        + _gdelt_llm_discovery_signals(db)
        + _gfw_anomaly_signals(db)
        + usace_signals
        + _tariff_policy_signals()
        + _latin_america_import_signals()
        + _asia_import_signals()
        + _gdelt_avian_flu_signals(db)
        + _gdelt_tariff_relief_signals(db)
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

    # Pre-pass, not per-event -- ranking "who has the biggest volume"
    # needs every matching shipment totaled up first, same reasoning as
    # _detect_mixed_sourcing_companies above. Scoped to just the
    # commodities with an active target this sweep, not all 35 --
    # cheap, since that set is normally small.
    active_commodities = {t.commodity for t in targets}
    volume_rankings = {c: _rank_importers_by_volume(db, c) for c in active_commodities}

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
            # Standing tariff check -- runs on every shipment regardless
            # of whether a MacroSignal fired this sweep (see
            # _tariff_trade_diversion_note's docstring on why this isn't
            # signal-gated the way the two checks above are).
            tariff_note, _tariff_signal_type = _tariff_trade_diversion_note(event.payload)
            latam_note, _latam_signal_type = _latin_america_sourcing_note(event.payload)
            asia_note, _asia_signal_type = _asia_sourcing_note(event.payload)
            # Real measured volume, not a guess -- only checked against
            # commodities with an active target this sweep (see the
            # pre-pass above), so this never claims a volume rank for a
            # commodity that isn't actually in play.
            volume_note = ""
            for commodity in active_commodities:
                found = _volume_rank_note(commodity, str(name), volume_rankings[commodity])
                if found:
                    volume_note = found
                    break
            standing_note = f"{tariff_note} -- " if tariff_note else ""
            standing_note += f"{latam_note} -- " if latam_note else ""
            standing_note += f"{asia_note} -- " if asia_note else ""
            standing_note += f"{volume_note} -- " if volume_note else ""
            trade_link_note = f"{port_link_note}{goods_link_note}{standing_note}"
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


def _active_revenue_check(company_name: str) -> tuple[float, bool, str]:
    """The ACTIVE counterpart to _stage3b_revenue_range_filter above --
    that one only ever scans fresh Apollo.io telemetry events already
    sitting in the database from an unrelated sweep, which is nearly
    useless for a passive Import Genius consignee with no reason to
    have ever shown up in the fixed Chicago sweep or a disruption hunt.
    This calls Apollo's per-company enrich endpoint directly, by name,
    right now -- the real "scan Import Genius, then call Apollo and
    cross-reference the revenue" check.

    Same scoring and honesty posture as the passive version: never
    dismisses for lack of data, only a CONFIRMED revenue figure outside
    settings.mca_target_revenue_min_usd/max_usd disqualifies a
    candidate. No API key configured, or no answer back from Apollo,
    both mean "unconfirmed," not "small" -- same as the passive check's
    "pending" branch. Returns (score_delta, dismiss, note)."""
    if not company_name.strip():
        return 0.0, False, "revenue check skipped -- no company name to match"

    settings = get_settings()
    if not settings.apollo_api_key:
        return 0.0, False, "revenue check pending -- no Apollo.io API key configured"

    adapter = ApolloAdapter(api_key=settings.apollo_api_key)
    try:
        org = adapter.enrich_organization(company_name)
    except Exception:
        return 0.0, False, "revenue check pending -- Apollo.io enrich call failed this sweep"

    if not org:
        return 0.0, False, f"revenue check pending -- Apollo.io has no record for '{company_name}' yet"

    revenue = _extract_revenue(org)
    if revenue is None:
        return 0.0, False, f"Apollo.io matched '{company_name}' but no revenue figure in payload -- band unconfirmed"

    if settings.mca_target_revenue_min_usd <= revenue <= settings.mca_target_revenue_max_usd:
        return (
            20.0,
            False,
            f"BIG-TICKET CONFIRMED: Apollo.io revenue ${revenue:,.0f} for '{company_name}' -- within target "
            f"${settings.mca_target_revenue_min_usd:,.0f}-${settings.mca_target_revenue_max_usd:,.0f} band",
        )
    return -30.0, True, (
        f"Apollo.io revenue ${revenue:,.0f} for '{company_name}' -- outside target "
        f"${settings.mca_target_revenue_min_usd:,.0f}-${settings.mca_target_revenue_max_usd:,.0f} band, dismissed"
    )


# Real-world grounding for this, direct from the business, not a guess:
# cattle ranchers running 500-1,000+ head, major grain players, and
# beef/potash/fertilizer importers moving heavy container volume are all
# genuinely in the target revenue band. Apollo's firmographic revenue
# field is frequently EMPTY for exactly these businesses -- closely-held,
# privately-run operations with no reason to ever report revenue to a
# public B2B database -- which is why _stage3b_revenue_range_filter and
# _active_revenue_check so often come back "pending" for them, not a
# true negative. That "pending" result was previously a dead end: no
# bonus, no proxy, just silence on revenue fit for exactly the
# businesses most likely to need Apollo's gap filled.
#
# Container/shipment volume is a REAL, already-available substitute --
# Import Genius and SeaVantage both carry a literal shipment count for
# any company that shows up in them at all, no third-party estimate
# required. The threshold below is a reasoned starting point, not a
# precise calculation -- a single ocean container typically carries
# tens of thousands of dollars of goods value, so a company moving this
# many containers in one sweep's fresh-event window is a real, scaling
# trade operation, consistent with (not proof of) the target band.
# Tune this against _signal_performance_report's real docs-in/funded
# rate once there's real outcome data behind it, same as every other
# reasoned constant in this file.
_CARGO_VOLUME_REVENUE_PROXY_MIN_SHIPMENTS = 5
# Smaller than BIG-TICKET CONFIRMED's +20 -- a real proxy off actual
# trade volume, but still a proxy, not a confirmed revenue figure.
_CARGO_VOLUME_REVENUE_PROXY_SCORE_BONUS = 10.0


def _cargo_volume_revenue_proxy(db: Session, company_name: str) -> tuple[float, bool, str]:
    """Fallback for _stage3b_revenue_range_filter/_active_revenue_check --
    ONLY meant to be called when Apollo came back with no decisive
    answer (score_delta 0.0, dismiss False: no match, or matched with no
    revenue figure), never when Apollo already gave a confirmed in-band
    or out-of-band answer. See both callers for the exact gating.

    Counts this company's own distinct Import Genius + SeaVantage
    shipments seen this sweep as a real, already-available scale signal
    -- no third-party revenue estimate required, just real trade volume
    this project already ingests. Never dismisses -- a low shipment
    count just means the proxy has nothing to say, the same honesty
    posture as the Apollo check it's filling a gap for; this only ever
    adds confidence, it never subtracts it."""
    if not company_name.strip():
        return 0.0, False, "cargo-volume revenue proxy skipped -- no company name to match"

    interzoid_key = get_settings().interzoid_api_key
    shipment_count = 0
    for source in (TelemetrySource.IMPORT_GENIUS, TelemetrySource.SEAVANTAGE):
        for event in _fresh_events(db, source, limit=200):
            matched_name = _company_name_from_event(event)
            if entity_matching.names_match(interzoid_key, company_name, matched_name):
                shipment_count += 1

    if shipment_count >= _CARGO_VOLUME_REVENUE_PROXY_MIN_SHIPMENTS:
        return (
            _CARGO_VOLUME_REVENUE_PROXY_SCORE_BONUS,
            False,
            f"CARGO VOLUME REVENUE PROXY: {shipment_count} real Import Genius/SeaVantage shipments this sweep -- "
            f"real trade volume consistent with the target revenue band, used because Apollo had no confirmed "
            f"figure for this company",
        )
    return (
        0.0,
        False,
        f"cargo-volume revenue proxy: only {shipment_count} shipment(s) seen this sweep -- not enough to use as a scale signal",
    )


# Bill-of-lading arrival/discharge date field name isn't confirmed
# against Import Genius's live schema (same posture as every other
# unconfirmed field in this module) -- check every plausible variant.
_ARRIVAL_DATE_KEYS = ("arrival_date", "discharge_date", "vessel_arrival_date", "unlading_date", "actual_arrival_date", "eta")

# SeaVantage's real Cargo Report product (confirmed against
# seavantage.com, NOT just their own adapter's earlier placeholder
# docstring) tracks a per-shipment "last free day" directly -- the
# actual contractual demurrage deadline for THAT container, not a
# guessed default. When present, this is strictly more precise than
# computing dwell against _DEMURRAGE_FREE_TIME_DAYS below, so it's
# checked FIRST.
_LAST_FREE_DAY_KEYS = ("last_free_day", "lfd", "free_time_expiry", "demurrage_start_date")

# Typical US port free-time window before demurrage/detention charges
# start accruing -- the real range is roughly 3-7 days depending on
# port/carrier/contract. A reasoned middle default, not a measured one,
# same honesty posture as every other threshold in this module --
# tune down if your actual deals skew toward tighter-free-time ports.
# Only used as a FALLBACK when no confirmed last-free-day field is
# present (see _LAST_FREE_DAY_KEYS above).
_DEMURRAGE_FREE_TIME_DAYS = 5

# A standalone bonus, not folded into cross_source/macro_corroboration
# -- this signal doesn't need a second source to be real. A container
# sitting past free time IS the capital need, confirmed by one source
# (the manifest's own dates), same confidence tier as a confirmed
# revenue-band match. A CONFIRMED last-free-day hit (not an estimate)
# scores higher -- it's a measured fact, not a 5-day guess.
_DEMURRAGE_SCORE_BONUS = 15.0
_DEMURRAGE_CONFIRMED_SCORE_BONUS = 22.0

# Same confidence tier as a confirmed revenue-band match -- a known
# country of origin is a measured fact about this specific shipment
# (see _tariff_trade_diversion_note), not an industry-level guess.
_TARIFF_CLASSIFICATION_SCORE_BONUS = 12.0

# Same confidence tier, same reasoning -- see _latin_america_sourcing_note.
_LATIN_AMERICA_SOURCING_SCORE_BONUS = 12.0
# Same confidence tier, same reasoning -- see _asia_sourcing_note.
_ASIA_SOURCING_SCORE_BONUS = 12.0

# Higher than the plain tariff bonus -- a company mid-diversification
# across BOTH sides of a tariff is a sharper, more specific read than
# either side alone (see _detect_mixed_sourcing_companies).
_MIXED_SOURCING_SCORE_BONUS = 18.0


def _extract_arrival_date(payload: dict) -> date | None:
    for key in _ARRIVAL_DATE_KEYS:
        value = payload.get(key)
        if not value:
            continue
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            continue
    return None


def _extract_last_free_day(payload: dict) -> date | None:
    for key in _LAST_FREE_DAY_KEYS:
        value = payload.get(key)
        if not value:
            continue
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            continue
    return None


def _demurrage_risk_note(payload: dict) -> tuple[str, bool]:
    """The sharpest trigger in this module, and the only one that needs
    no industry-level story at all: a container sitting at port beyond
    its carrier's free-time window accrues a running DAILY fee
    regardless of how healthy the underlying business is. "Your
    container's been sitting N days past free time, a bridge covers
    the fee and gets your goods moving" is a complete, fundable pitch
    on this one data point -- no corroboration from a second source
    needed, unlike every other signal in this module.

    Two tiers, checked in order of confidence:
    1. CONFIRMED -- a real last-free-day field is present (SeaVantage's
       Cargo Report product carries this; see its adapter's docstring)
       and has already passed. This is a measured fact about THIS
       specific shipment's actual contract terms, not a guess.
    2. ESTIMATED -- no confirmed field, so dwell time is computed from
       the shipment's own arrival/discharge date against the reasoned
       _DEMURRAGE_FREE_TIME_DAYS default (this is what an Import
       Genius-only record falls back to, since customs manifests don't
       carry a carrier's actual free-time terms).

    Returns (note, confirmed) -- confirmed=True only for tier 1, which
    callers use to apply the higher score bonus. ("", False) when
    there's no date to compute from, or dwell time is still within the
    free-time window -- never a dismiss, just no signal yet."""
    last_free_day = _extract_last_free_day(payload)
    if last_free_day:
        # A confirmed field is strictly better information than the
        # arrival-date estimate below, whichever way it reads -- if
        # it's already past, that's the CONFIRMED signal; if it
        # hasn't passed yet, that's a confirmed "not yet", which must
        # short-circuit here rather than falling through to a cruder
        # guess that might fire anyway on a stale arrival_date in the
        # same payload.
        if date.today() > last_free_day:
            overage_days = (date.today() - last_free_day).days
            return (
                f"DEMURRAGE RISK (CONFIRMED): last free day was {last_free_day.isoformat()}, "
                f"{overage_days} day(s) ago -- this specific shipment's own contract terms, not an estimate -- "
                f"actively accruing demurrage/detention fees right now regardless of underlying financial "
                f"health -- needs no other corroboration to be a real bridge-capital pitch",
                True,
            )
        return "", False

    arrival = _extract_arrival_date(payload)
    if not arrival:
        return "", False
    dwell_days = (date.today() - arrival).days
    if dwell_days <= _DEMURRAGE_FREE_TIME_DAYS:
        return "", False
    overage_days = dwell_days - _DEMURRAGE_FREE_TIME_DAYS
    return (
        f"DEMURRAGE RISK (ESTIMATED): shipment arrived {arrival.isoformat()} ({dwell_days} days ago), "
        f"{overage_days} day(s) past the typical {_DEMURRAGE_FREE_TIME_DAYS}-day free-time window -- "
        f"likely accruing daily demurrage/detention fees right now regardless of underlying financial "
        f"health -- needs no other corroboration to be a real bridge-capital pitch",
        False,
    )


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
        if revenue_delta == 0.0 and not revenue_dismiss:
            # Apollo had no decisive answer -- fall back to real
            # container/shipment volume before giving up on revenue fit
            # entirely. See _cargo_volume_revenue_proxy's own docstring.
            proxy_delta, _proxy_dismiss, proxy_note = _cargo_volume_revenue_proxy(db, company_name)
            if proxy_delta:
                revenue_delta, revenue_note = proxy_delta, proxy_note
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
        # Same confidence tier either way -- a confirmed country of
        # origin is a measured fact about THIS shipment (see
        # _tariff_trade_diversion_note), not a hypothesis, whichever
        # side of the tariff it lands on.
        tariff_bonus = (
            _TARIFF_CLASSIFICATION_SCORE_BONUS
            if "TARIFF-AFFECTED SOURCING" in corroboration_note or "TARIFF-DIVERSION WINNER" in corroboration_note
            else 0.0
        )
        latam_bonus = _LATIN_AMERICA_SOURCING_SCORE_BONUS if "LATIN AMERICA SOURCING CONFIRMED" in corroboration_note else 0.0
        asia_bonus = _ASIA_SOURCING_SCORE_BONUS if "ASIA SOURCING CONFIRMED" in corroboration_note else 0.0
        volume_rank_bonus = _VOLUME_RANK_SCORE_BONUS if "LARGEST VOLUME IMPORTER" in corroboration_note else 0.0
        score_delta = (
            standing_delta
            + revenue_delta
            + cross_source_bonus
            + macro_corroboration_bonus
            + tariff_bonus
            + latam_bonus
            + asia_bonus
            + volume_rank_bonus
        )
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

    # Import Genius's passive consignee feed, AND HigherGov's named
    # awardee/recipient feed (when one is actually present -- see
    # _company_name_from_event's honesty note on HigherGov's unconfirmed
    # awards schema), get an ACTIVE per-company Apollo revenue check --
    # "scan the feed, then call Apollo and cross-reference the revenue"
    # -- so a candidate actually confirmed in the $5M-$49M band is
    # scored and labeled BIG-TICKET, and one confirmed well outside it
    # is dismissed instead of landing as an unscored, unfiltered
    # PENDING row same as everything else. This is the "highly
    # targeted list" play: a HigherGov contract win with a real named
    # company attached skips the broad industry sweep entirely and
    # goes straight to the same real-time revenue+standing check a
    # named Import Genius consignee gets.
    #
    # Scoped to these two named-entity sources only, not the other 5
    # flat-mapped sources -- this is one real Apollo call per NEW
    # candidate, a real per-call cost, deliberately not extended
    # everywhere without being asked. Deduped by name within this
    # sweep so two events for the same company don't double-charge.
    # Caches the full (score, status, notes) result per company name,
    # not just "was it checked" -- a company mapped to multiple silos,
    # or appearing in multiple fresh events this sweep, must get the
    # SAME result applied to every resulting row, not just whichever
    # (event, silo) pair happened to trigger the one real Apollo call.
    revenue_check_cache: dict[str, tuple[float | None, SiloCandidateStatus, str | None]] = {}

    # Pre-pass, not per-event -- detecting "mixed sourcing" needs to see
    # ALL of a company's shipments together before any row is written
    # (see _detect_mixed_sourcing_companies).
    mixed_sourcing_companies = _detect_mixed_sourcing_companies(db)

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

            company_name = _company_name_from_event(event)
            score, status, notes = None, SiloCandidateStatus.PENDING, None
            # HigherGov with no real awardee name just falls back to
            # event.title (the opportunity's own title) -- running that
            # through Apollo's company-enrich endpoint would waste a
            # call on a non-company string, so this only fires for
            # HigherGov when a real name was actually extracted.
            is_named_highergov = event.source == TelemetrySource.HIGHERGOV and company_name != event.title
            if event.source == TelemetrySource.IMPORT_GENIUS or is_named_highergov:
                if company_name not in revenue_check_cache:
                    score_delta, dismiss, note = _active_revenue_check(company_name)
                    if score_delta == 0.0 and not dismiss:
                        # Apollo had no decisive answer -- same
                        # container/shipment-volume fallback as the CME
                        # Macro Funnel waterfall uses.
                        proxy_delta, _proxy_dismiss, proxy_note = _cargo_volume_revenue_proxy(db, company_name)
                        if proxy_delta:
                            score_delta, note = proxy_delta, proxy_note
                    cached_score = 50.0 + score_delta if (score_delta or dismiss) else None
                    cached_status = SiloCandidateStatus.DISMISSED if dismiss else SiloCandidateStatus.PENDING
                    revenue_check_cache[company_name] = (cached_score, cached_status, note)
                score, status, notes = revenue_check_cache[company_name]

            # Demurrage risk is per-SHIPMENT, not per-company (unlike
            # the revenue check above) -- computed directly off this
            # event's own payload, no cache needed, and independent of
            # whether any macro signal is active this sweep (the CME
            # Macro Funnel waterfall below gates shut with zero active
            # signals; this never does, since a stuck container is a
            # real capital need on its own). Checked for BOTH Import
            # Genius (customs manifest dates, estimated tier) and
            # SeaVantage (real last-free-day field, confirmed tier --
            # see _demurrage_risk_note).
            if event.source in (TelemetrySource.IMPORT_GENIUS, TelemetrySource.SEAVANTAGE):
                demurrage_note, demurrage_confirmed = _demurrage_risk_note(event.payload)
                if demurrage_note:
                    bonus = _DEMURRAGE_CONFIRMED_SCORE_BONUS if demurrage_confirmed else _DEMURRAGE_SCORE_BONUS
                    score = (score if score is not None else 50.0) + bonus
                    notes = f"{notes} | {demurrage_note}" if notes else demurrage_note

            # Tariff trade-diversion check -- same per-shipment, standing
            # posture as demurrage above. Checked for BOTH Import Genius
            # (customs country-of-origin field) and SeaVantage (real
            # per-container milestone data can carry an origin port/
            # country too -- see _COUNTRY_OF_ORIGIN_KEYS) -- a company
            # with mixed sourcing might only show the full picture
            # across both sources, not either one alone.
            if event.source in (TelemetrySource.IMPORT_GENIUS, TelemetrySource.SEAVANTAGE):
                tariff_note, _tariff_signal_type = _tariff_trade_diversion_note(event.payload)
                if tariff_note:
                    score = (score if score is not None else 50.0) + _TARIFF_CLASSIFICATION_SCORE_BONUS
                    notes = f"{notes} | {tariff_note}" if notes else tariff_note

                # Same standing-check posture as tariff above -- a real
                # Latin America trade partner in the country-of-origin
                # field is confirmation on its own, not gated on any
                # macro signal having fired this sweep.
                latam_note, _latam_signal_type = _latin_america_sourcing_note(event.payload)
                if latam_note:
                    score = (score if score is not None else 50.0) + _LATIN_AMERICA_SOURCING_SCORE_BONUS
                    notes = f"{notes} | {latam_note}" if notes else latam_note

                # Same standing-check posture as Latin America above --
                # see _asia_sourcing_note for why this never
                # double-fires against a country already covered by
                # the tariff mechanism for this same commodity.
                asia_note, _asia_signal_type = _asia_sourcing_note(event.payload)
                if asia_note:
                    score = (score if score is not None else 50.0) + _ASIA_SOURCING_SCORE_BONUS
                    notes = f"{notes} | {asia_note}" if notes else asia_note

                # Mixed-sourcing beats either single read -- see
                # _detect_mixed_sourcing_companies for why this scores
                # higher than the plain tariff bonus above.
                mixed_note = mixed_sourcing_companies.get(company_name)
                if mixed_note:
                    score = (score if score is not None else 50.0) + _MIXED_SOURCING_SCORE_BONUS
                    notes = f"{notes} | {mixed_note}" if notes else mixed_note

            db.add(
                SiloCandidate(
                    silo=silo,
                    company_name=company_name,
                    source_reference=source_reference,
                    origin_source=event.source,
                    score=score,
                    status=status,
                    notes=notes,
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


# ---------------------------------------------------------------------------
# Signal performance report -- "how does the brain evolve after a few
# funded deals or docs-in conversions." Every corroboration marker this
# file emits (tariff/Latin America/Asia sourcing, demurrage, mixed
# sourcing, macro-corroboration, cross-source confirmation, big-ticket
# revenue) gets written into SiloCandidate.notes as a fixed, literal
# string -- see each marker's own docstring/comment above. That means
# the real-world OUTCOME of every one of those markers is already sitting
# in the database the moment a candidate converts: SiloCandidate.
# converted_lead_uid points at the MasterLogEntry it became, whose status
# (including the real pipeline value "Docs in") and funding_events (the
# real, append-only funded-dollar history, supporting repeat funding)
# are the actual ground truth this system has been reasoning TOWARD this
# entire session without ever actually measuring. This is that
# measurement: it answers "which of our reasoned bonuses are real
# signal and which are noise that just happens to fire a lot," using
# real funded/docs-in outcomes, not another guess.
# ---------------------------------------------------------------------------

# The exact, literal marker strings every corroboration/standing check in
# this file writes into SiloCandidate.notes -- kept here as the single
# source of truth for the performance report so adding a new marker
# anywhere above means adding it here too, not a separate guessed regex.
_SIGNAL_PERFORMANCE_MARKERS = (
    "TARIFF-AFFECTED SOURCING",
    "TARIFF-DIVERSION WINNER",
    "MIXED SOURCING",
    "LATIN AMERICA SOURCING CONFIRMED",
    "ASIA SOURCING CONFIRMED",
    "DEMURRAGE RISK (CONFIRMED)",
    "DEMURRAGE RISK (ESTIMATED)",
    "BIG-TICKET CONFIRMED",
    "CARGO VOLUME REVENUE PROXY",
    "LARGEST VOLUME IMPORTER",
    "MACRO-CORROBORATED",
    "CROSS-SOURCE CONFIRMED",
)

# Every MasterLogStatus that means real paperwork actually came back from
# the merchant -- "Docs in" itself, plus everything downstream of it in
# the real pipeline (negotiation, an offer that didn't close, a sold deal
# that later fell through, or funded). Deliberately NOT
# DEAL_STALLED_PROXY_PASS -- that can stall before docs ever came in, so
# counting it here would overstate how many of these leads were real
# qualified interest, the same overclaim-avoidance posture as every
# "honest limit" elsewhere in this file.
_DOCS_IN_OR_FURTHER_STATUSES = frozenset(
    {
        MasterLogStatus.DOCS_IN,
        MasterLogStatus.IN_NEGOTIATION,
        MasterLogStatus.OFFER_MADE_NOT_SOLD,
        MasterLogStatus.SOLD_DEAL_KILLED,
        MasterLogStatus.FUNDED,
    }
)


def _signal_performance_report(db: Session) -> list[dict]:
    """Empirical conversion/funding performance per signal marker,
    measured against real outcomes, not reasoned assumptions.

    For every CONVERTED SiloCandidate that actually became a Master Log
    lead, this walks that lead's real status and real funding_events
    history and attributes the outcome back to every marker string
    present in the candidate's own notes (a candidate can carry more than
    one marker -- e.g. TARIFF-AFFECTED SOURCING and CROSS-SOURCE
    CONFIRMED on the same shipment -- and gets counted under each one it
    actually carries). A candidate with NONE of the known markers is
    counted under "UNTAGGED" -- the baseline every real marker needs to
    beat; a marker with a worse docs-in/funded rate than UNTAGGED is
    noise, not signal, whatever its score bonus currently assumes.

    Returns one row per marker (plus "UNTAGGED" and one row per
    origin_source actually seen), each with:
      - total_candidates: how many converted candidates carried this tag
      - docs_in_or_further: how many of those reached real paperwork
        (see _DOCS_IN_OR_FURTHER_STATUSES)
      - funded_count: how many actually funded at least once
      - total_funded_amount: real dollars funded across every funding
        event tied to this tag's candidates (repeat funding counts every
        event, not just the first)
      - docs_in_rate / funded_rate: the two numbers that actually answer
        "is this marker worth its score bonus"

    Deliberately read-only and append-nothing -- this doesn't change any
    score or bonus itself. The point is to hand a human (or a future
    automated tuning pass) the real numbers needed to decide which
    _xxx_SCORE_BONUS constants above deserve to go up, down, or be cut
    entirely, after real deals, not before."""
    converted = (
        db.query(SiloCandidate)
        .filter(SiloCandidate.status == SiloCandidateStatus.CONVERTED, SiloCandidate.converted_lead_uid.isnot(None))
        .all()
    )

    stats: dict[str, dict] = {}

    def _bucket(key: str) -> dict:
        return stats.setdefault(
            key, {"total_candidates": 0, "docs_in_or_further": 0, "funded_count": 0, "total_funded_amount": 0.0}
        )

    for candidate in converted:
        lead = db.get(MasterLogEntry, candidate.converted_lead_uid)
        if lead is None:
            continue  # FK is ON DELETE SET NULL, but defensive regardless

        docs_in_or_further = lead.status in _DOCS_IN_OR_FURTHER_STATUSES
        funding_events = lead.funding_events
        funded = len(funding_events) > 0
        funded_amount = sum(float(fe.funded_amount) for fe in funding_events)

        notes = candidate.notes or ""
        matched_markers = [marker for marker in _SIGNAL_PERFORMANCE_MARKERS if marker in notes]
        tags = matched_markers or ["UNTAGGED"]
        if candidate.origin_source:
            tags = tags + [f"source:{candidate.origin_source.value}"]

        for tag in tags:
            bucket = _bucket(tag)
            bucket["total_candidates"] += 1
            bucket["docs_in_or_further"] += int(docs_in_or_further)
            bucket["funded_count"] += int(funded)
            bucket["total_funded_amount"] += funded_amount

    report = []
    for tag, bucket in stats.items():
        total = bucket["total_candidates"]
        report.append(
            {
                "tag": tag,
                "total_candidates": total,
                "docs_in_or_further": bucket["docs_in_or_further"],
                "funded_count": bucket["funded_count"],
                "total_funded_amount": round(bucket["total_funded_amount"], 2),
                "docs_in_rate": round(bucket["docs_in_or_further"] / total, 3) if total else 0.0,
                "funded_rate": round(bucket["funded_count"] / total, 3) if total else 0.0,
            }
        )
    report.sort(key=lambda r: (r["funded_rate"], r["total_candidates"]), reverse=True)
    return report


# ---------------------------------------------------------------------------
# Daily continuous sweep -- "a living lead-gen organism that sweeps
# daily" means run_silo_leadgen can't only ever run when a human calls
# POST /api/silo/leadgen/run. Same off-by-default-until-configured
# pattern as sheets_sync.py/calendar_sync.py's background pulls: several
# of the sources this sweep hits (Apollo, Cobalt, CME Globex) are real
# paid external APIs, not free local computation, so this stays opt-in
# rather than running the moment the server starts.
# ---------------------------------------------------------------------------

_sweep_task = None


async def _sweep_loop():
    import asyncio

    from app.database import SessionLocal

    while True:
        try:
            db = SessionLocal()
            try:
                result = await asyncio.to_thread(run_silo_leadgen, db)
                logger.info("Silo lead-gen sweep: candidates_created=%s", result["candidates_created"])
            finally:
                db.close()
        except Exception:  # noqa: BLE001 -- one bad cycle must not kill the loop
            logger.exception("Silo lead-gen sweep failed -- will retry next cycle")
        await asyncio.sleep(get_settings().silo_leadgen_sweep_interval_seconds)


def start_background_sweep():
    """Off by default (silo_leadgen_continuous_sweep_enabled) -- set
    SILO_LEADGEN_CONTINUOUS_SWEEP_ENABLED=true once the real API keys
    (Apollo, Cobalt, Databento, HigherGov, Datalastic/VesselFinder) are
    actually configured, so the first automatic sweep isn't a no-op."""
    import asyncio

    global _sweep_task
    if not get_settings().silo_leadgen_continuous_sweep_enabled:
        logger.info("Silo lead-gen continuous sweep disabled (set SILO_LEADGEN_CONTINUOUS_SWEEP_ENABLED=true to activate)")
        return None
    if _sweep_task is None or _sweep_task.done():
        _sweep_task = asyncio.create_task(_sweep_loop())
        logger.info("Silo lead-gen background sweep started (interval=%ss)", get_settings().silo_leadgen_sweep_interval_seconds)
    return _sweep_task


def stop_background_sweep() -> None:
    global _sweep_task
    if _sweep_task is not None:
        _sweep_task.cancel()
        _sweep_task = None
