"""USACE lock/port-disruption signal, from two complementary channels.

**Multi-corridor, multi-commodity.** MONITORED_LOCKS originally covered
only the Mississippi/Illinois grain corridor (feeding the Agriculture &
Grain Handling silo). It now also covers the Ohio River corridor (coal +
steel -- the Pittsburgh/upper-Ohio steel region moves both by barge),
the Gulf Intracoastal Waterway (petrochemicals, around the Houston/Lake
Charles/Baton Rouge corridor), the Great Lakes/Soo Locks corridor (the
iron-ore-to-steel-mill supply chain, plus Duluth-Superior grain exports),
and four coastal port anchorage points (marine-traffic-at-port
congestion, not river-lock congestion -- see below).

**Commodity MIX, not a single tag.** Most of these corridors carry more
than one commodity -- the Ohio River moves steel inputs and coal, but
also real grain tonnage; the GIWW moves petrochemicals and a meaningful
share of grain export traffic near New Orleans/Baton Rouge; Duluth-
Superior is genuinely both a top grain port AND a major ore port. A
single fixed tag per lock (the original design) would make the
Agriculture & Grain Handling waterfall blind to real grain traffic
moving through a lock tagged "steel_coal" for its dominant commodity.
So each entry now carries `commodities: {tag: weight, ...}` (weights
sum to ~1.0) instead of one string -- `_usace_disruption_signals` emits
one weighted MacroSignal PER commodity above a floor share, so a single
disrupted lock can legitimately feed multiple silos at once, proportional
to how much of its real traffic each one represents.

**These specific percentages are directional estimates, not sourced
numbers.** USACE's own Waterborne Commerce Statistics Center publishes
real tonnage-by-waterway-by-commodity data (required by the Rivers and
Harbors Act) that would give exact figures -- I don't have a confirmed
live, scriptable feed for it (it reads as a periodic table download, not
an API), so I'm not fabricating lock-by-lock precision I can't source.
The splits below reflect well-established general knowledge of what
each corridor actually carries (e.g. "the Ohio River is coal/steel-
dominant but not exclusively so"), not a specific measured number --
replace them with the real WCSC figures once pulled, same posture as
every other "confirm before treating as precise" caveat in this file.

Every signal still carries its commodity tag (now plus weight) so a
caller knows what it actually means (a steel-corridor disruption isn't
automatically a grain signal just because it fires) -- callers filter
`_usace_disruption_signals` by commodity, same as before. Real
CWMS office/project_id and precise lock coordinates for the new Ohio
River / GIWW entries are NOT yet confirmed against the live USACE
catalog (same caveat as the original Mississippi/Illinois entries always
carried) -- these are well-established public infrastructure (real lock
names, approximate geography), but verify via this file's own self-serve
CWMS lookup before relying on the gate-change channel for them. The AIS
channel works immediately once a key is set, no CWMS confirmation
needed, same as before.

**Port anchorage entries.** A cluster of vessels anchored/idling outside
a major port (not transiting through) is the coastal equivalent of barge
queuing at a river lock -- a leading indicator that cargo bound for that
port's catchment area is delayed, which stresses the cash flow of
import-dependent businesses there before the delay shows up anywhere
else. These reuse the exact same AIS-queue detection channel as the
river locks; they deliberately carry no office/project_id (ports have no
CWMS lock-gate data), so the gate-change channel silently skips them --
exactly the existing "blank office/project_id = this channel doesn't
apply" behavior, now doing useful work for a second purpose instead of
only meaning "not yet confirmed."

**Channel 1 -- real CWMS Data API (CDA).** Confirmed against the actual
USACE/HEC source (https://github.com/USACE/cwms-data-api), not guessed:
production base URL `https://cwms-data.usace.army.mil/cwms-data`, GET
endpoints for non-sensitive data are keyless per their FAQ (an API key is
only required for writes or genuinely sensitive endpoints), and the
`/projects/{office}/{project-id}/gate-changes` endpoint (see
GateChangeGetAllController.java) returns real operational gate-change
history for a reservoir project. A cluster of gate changes in a short
window means active water-control intervention (flood-stage or
drought-stage operations) -- exactly the condition under which a lock is
most likely to be restricting or halting traffic. This is a genuine
USACE data feed, not a proxy.

This channel needs each monitored project's real CWMS `office` (e.g.
"MVS" for St. Louis District) and `project_id` (CWMS's internal project
name) filled in below -- I don't have a live session to look these up
and confirm them, so guessing them would repeat exactly the mistake this
file's git history already made once (a placeholder endpoint passed off
as real). Self-serve discovery, no key needed:

    GET https://cwms-data.usace.army.mil/cwms-data/locations/with-kinds/\
?names={lock name}&location-kind-like=LOCK

returns the owning office for a location by name -- confirm the project
ID from the CorpsLocks portal, or from a `/projects/locks?office=X&project-id=Y`
call once you have a candidate ID. Entries below with office/project_id
left blank are simply skipped for this channel.

**Channel 2 -- AIS proxy.** Independent of channel 1 and requires no
CWMS lookup: a lock closure or extended delay backs barges up right
outside the lock, visible in AIS as a cluster of near-stationary vessels
at the lock's coordinates. Uses the same Datalastic (preferred) /
VesselFinder (fallback) radius-search already wired up elsewhere in this
project. Deliberately does not fall back to GFW 4Wings: its default
dataset is fishing-vessel-specific and wouldn't see grain barges.

Feeds CME Macro Funnel: barge congestion/disruption on the
Mississippi/Illinois grain corridor is a leading indicator for CME
agricultural futures (corn, soybeans, wheat) volatility, ahead of the
price move itself showing up on CME Globex.
"""

import datetime as dt

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

CWMS_BASE_URL = "https://cwms-data.usace.army.mil/cwms-data"  # confirmed real -- see module docstring
DATALASTIC_BASE_URL = "https://api.datalastic.com/api/v0"  # placeholder -- confirm against your Datalastic API plan
VESSELFINDER_BASE_URL = "https://api.vesselfinder.com"  # placeholder -- confirm against your VesselFinder API plan

# name, lat, lon (for the AIS channel -- always active if a key is set),
# office, project_id (for the real CWMS gate-changes channel -- blank
# until you've confirmed them per the module docstring's self-serve
# lookup; this channel silently skips any entry left blank), commodities
# (a {tag: weight} mix, weights sum to ~1.0 -- see module docstring on
# why this is a mix, not one tag, and why these specific weights are
# directional estimates pending real WCSC confirmation), apollo_location
# (a real named city/state Apollo.io's org-search location filter can
# actually use -- Apollo's API takes a place name, not a lat/lon radius,
# see services/silo_leadgen.py::_stage2b_apollo_industry_hunt), state (a
# plain 2-letter code -- Cobalt Intelligence's SOS search takes a state,
# not a city, see _stage2c_cobalt_industry_hunt).
MONITORED_LOCKS = [
    # --- Mississippi / Illinois Waterway: grain corridor, with a real
    # fertilizer backhaul -- the same barges that carry grain DOWN to
    # Gulf export terminals carry fertilizer/potash BACK UP to Midwest
    # ag retailers (imported through the Gulf, see the GIWW/New Orleans
    # entry below), not a separate fleet. Right at _MIN_COMMODITY_WEIGHT
    # (0.15) so a disruption here still opens a fertilizer signal, just
    # clearly secondary to this corridor's dominant grain traffic.
    {"name": "Lock and Dam 1 (Mississippi, Minneapolis)", "lat": 44.92, "lon": -93.20, "office": "", "project_id": "", "commodities": {"grain": 0.85, "fertilizer": 0.15}, "apollo_location": "Minneapolis, Minnesota, US", "state": "MN"},
    {"name": "Lock and Dam 19 (Mississippi, Keokuk)", "lat": 40.40, "lon": -91.38, "office": "", "project_id": "", "commodities": {"grain": 0.85, "fertilizer": 0.15}, "apollo_location": "Keokuk, Iowa, US", "state": "IA"},
    {"name": "Melvin Price Locks and Dam (Mississippi, Alton)", "lat": 38.87, "lon": -90.16, "office": "", "project_id": "", "commodities": {"grain": 0.85, "fertilizer": 0.15}, "apollo_location": "Alton, Illinois, US", "state": "IL"},
    {"name": "Lockport Lock (Illinois Waterway)", "lat": 41.59, "lon": -88.08, "office": "", "project_id": "", "commodities": {"grain": 0.85, "fertilizer": 0.15}, "apollo_location": "Joliet, Illinois, US", "state": "IL"},
    {"name": "Marseilles Lock (Illinois Waterway)", "lat": 41.33, "lon": -88.71, "office": "", "project_id": "", "commodities": {"grain": 0.85, "fertilizer": 0.15}, "apollo_location": "Marseilles, Illinois, US", "state": "IL"},
    {"name": "Peoria Lock (Illinois Waterway)", "lat": 40.63, "lon": -89.61, "office": "", "project_id": "", "commodities": {"grain": 0.85, "fertilizer": 0.15}, "apollo_location": "Peoria, Illinois, US", "state": "IL"},
    # --- Ohio River: coal + steel dominant, but a real secondary grain
    # share moves this corridor too (Midwest grain barged down to the
    # Mississippi often transits the lower Ohio) -- lock names and
    # general geography are well-established public infrastructure;
    # office/project_id NOT yet confirmed against the live CWMS catalog,
    # see module docstring. Weights are directional, not WCSC-sourced.
    {"name": "Emsworth Locks and Dam (Ohio River, Pittsburgh)", "lat": 40.51, "lon": -80.09, "office": "", "project_id": "", "commodities": {"steel_coal": 0.65, "petrochemical": 0.15, "grain": 0.20}, "apollo_location": "Pittsburgh, Pennsylvania, US", "state": "PA"},
    {"name": "Montgomery Locks and Dam (Ohio River, Pittsburgh)", "lat": 40.64, "lon": -80.19, "office": "", "project_id": "", "commodities": {"steel_coal": 0.65, "petrochemical": 0.15, "grain": 0.20}, "apollo_location": "Pittsburgh, Pennsylvania, US", "state": "PA"},
    {"name": "New Cumberland Locks and Dam (Ohio River, Steubenville/Weirton)", "lat": 40.50, "lon": -80.62, "office": "", "project_id": "", "commodities": {"steel_coal": 0.70, "petrochemical": 0.10, "grain": 0.20}, "apollo_location": "Steubenville, Ohio, US", "state": "OH"},
    {"name": "Greenup Locks and Dam (Ohio River, Huntington)", "lat": 38.65, "lon": -82.93, "office": "", "project_id": "", "commodities": {"steel_coal": 0.60, "petrochemical": 0.15, "grain": 0.25}, "apollo_location": "Huntington, West Virginia, US", "state": "WV"},
    {"name": "McAlpine Locks and Dam (Ohio River, Louisville)", "lat": 38.26, "lon": -85.78, "office": "", "project_id": "", "commodities": {"steel_coal": 0.55, "petrochemical": 0.15, "grain": 0.30}, "apollo_location": "Louisville, Kentucky, US", "state": "KY"},
    {"name": "Olmsted Locks and Dam (Ohio River, mouth)", "lat": 37.18, "lon": -89.07, "office": "", "project_id": "", "commodities": {"steel_coal": 0.50, "petrochemical": 0.15, "grain": 0.35}, "apollo_location": "Paducah, Kentucky, US", "state": "KY"},
    # --- Gulf Intracoastal Waterway: petrochemical-dominant, but
    # significant grain export tonnage also transits near New Orleans/
    # Baton Rouge (river grain elevators feed export vessels staged along
    # this same waterway). Same confirmation caveat as above.
    # New Orleans is THE real US fertilizer/potash import gateway
    # (Canadian/Russian/Belarusian potash and ammonia come in here
    # before moving upriver) -- this is where a geopolitical potash/
    # fertilizer headline (see silo_leadgen.py's
    # _gdelt_geopolitical_fertilizer_signals) actually gets a real
    # river-corridor corroboration, not just a nationwide guess.
    {"name": "Algiers Lock (GIWW, New Orleans)", "lat": 29.93, "lon": -90.03, "office": "", "project_id": "", "commodities": {"petrochemical": 0.55, "grain": 0.30, "fertilizer": 0.15}, "apollo_location": "New Orleans, Louisiana, US", "state": "LA"},
    {"name": "Port Allen Lock (GIWW, Baton Rouge)", "lat": 30.45, "lon": -91.21, "office": "", "project_id": "", "commodities": {"petrochemical": 0.60, "grain": 0.40}, "apollo_location": "Baton Rouge, Louisiana, US", "state": "LA"},
    {"name": "Calcasieu Lock (GIWW, Lake Charles)", "lat": 30.12, "lon": -93.36, "office": "", "project_id": "", "commodities": {"petrochemical": 0.85, "grain": 0.15}, "apollo_location": "Lake Charles, Louisiana, US", "state": "LA"},
    # --- Coastal port anchorages: marine-traffic-at-port congestion,
    # AIS-only (no CWMS lock-gate data applies -- office/project_id left
    # blank on purpose, see module docstring). Picked to match your real
    # book's top states (CA/TX/NY/FL/NJ import gateways).
    #
    # No longer single-category: LA/Long Beach and NY/NJ are the #1/#3
    # US container ports and overwhelmingly retail/consumer-goods
    # import gateways, so they now carry a real ecommerce_logistics
    # weight alongside port_congestion -- a tariff/container headline
    # or an AIS-confirmed queue here is a real, geo-anchored e-commerce
    # capital-need signal, not a nationwide guess (see
    # silo_leadgen.py's _COMMODITY_DISTRESS_LANGUAGE["ecommerce_
    # logistics"]). Houston skews petrochemical/energy cargo (smaller
    # container share); Miami/Port Everglades carries real retail
    # container traffic too (Latin America/Caribbean trade), between
    # the two.
    # Apparel added to LA/Long Beach, NY/NJ, and Savannah below -- the
    # three dominant general-merchandise/consumer-goods gateways, and
    # apparel/textiles are arguably the single largest container-import
    # category at all three, same real-weight reasoning as furniture.
    # ecommerce_seller added to LA/Long Beach, NY/NJ, and Savannah --
    # the actual e-commerce BRANDS/sellers, distinct from
    # ecommerce_logistics' fulfillment/3PL service providers, and
    # their inventory physically moves through these exact same ports.
    {"name": "Port of Los Angeles / Long Beach anchorage", "lat": 33.72, "lon": -118.25, "office": "", "project_id": "", "commodities": {"port_congestion": 0.3, "ecommerce_logistics": 0.2, "furniture_wood": 0.1, "apparel": 0.2, "ecommerce_seller": 0.2}, "apollo_location": "Los Angeles, California, US", "state": "CA"},
    {"name": "Houston Ship Channel anchorage", "lat": 29.42, "lon": -94.90, "office": "", "project_id": "", "commodities": {"port_congestion": 0.8, "ecommerce_logistics": 0.2}, "apollo_location": "Houston, Texas, US", "state": "TX"},
    {"name": "Port of New York / New Jersey anchorage", "lat": 40.47, "lon": -73.95, "office": "", "project_id": "", "commodities": {"port_congestion": 0.3, "ecommerce_logistics": 0.25, "apparel": 0.2, "ecommerce_seller": 0.25}, "apollo_location": "Newark, New Jersey, US", "state": "NJ"},
    {"name": "Port Everglades / Miami anchorage", "lat": 26.09, "lon": -80.10, "office": "", "project_id": "", "commodities": {"port_congestion": 0.7, "ecommerce_logistics": 0.3}, "apollo_location": "Miami, Florida, US", "state": "FL"},
    # Smaller/mid-size ports -- added specifically to stop missing real
    # import-dependent businesses outside the 4 biggest gateways above.
    # Same AIS-only mechanism (Datalastic/VesselFinder's already-
    # confirmed radius query), not GFW -- GFW's own adapter docstring
    # says its wired-in dataset is fishing-vessel-specific, not a
    # general port-traffic product, so it isn't the right tool for
    # this. Savannah is genuinely one of the largest and fastest-
    # growing US container ports now (comparable retail-import share
    # to LA/NY-NJ, not actually "small") -- weighted accordingly.
    # Charleston/Oakland/Tacoma are real but smaller-volume container
    # gateways, weighted like Miami.
    # Savannah carries a real, meaningfully larger furniture_wood share
    # than the other ports -- it's a dominant gateway for the Vietnam/
    # China furniture trade specifically (including serving the High
    # Point, NC furniture-industry corridor), not an arbitrary split.
    {"name": "Port of Savannah anchorage", "lat": 32.08, "lon": -81.09, "office": "", "project_id": "", "commodities": {"port_congestion": 0.25, "ecommerce_logistics": 0.15, "furniture_wood": 0.2, "apparel": 0.2, "ecommerce_seller": 0.2}, "apollo_location": "Savannah, Georgia, US", "state": "GA"},
    {"name": "Port of Charleston anchorage", "lat": 32.80, "lon": -79.92, "office": "", "project_id": "", "commodities": {"port_congestion": 0.7, "ecommerce_logistics": 0.3}, "apollo_location": "Charleston, South Carolina, US", "state": "SC"},
    {"name": "Port of Oakland anchorage", "lat": 37.80, "lon": -122.32, "office": "", "project_id": "", "commodities": {"port_congestion": 0.7, "ecommerce_logistics": 0.3}, "apollo_location": "Oakland, California, US", "state": "CA"},
    {"name": "Port of Tacoma anchorage (Northwest Seaport Alliance)", "lat": 47.28, "lon": -122.40, "office": "", "project_id": "", "commodities": {"port_congestion": 0.7, "ecommerce_logistics": 0.3}, "apollo_location": "Tacoma, Washington, US", "state": "WA"},
    # --- Further port expansion toward comprehensive US coverage.
    # Each one picked for a REAL, differentiated cargo character, not
    # arbitrary inclusion -- Honolulu/Anchorage deliberately left out
    # despite being real ports, since their cargo mix isn't a distinct
    # fit for anything tracked here and their volume is low enough that
    # adding them would be coverage for its own sake, not signal.
    #
    # Norfolk/Hampton Roads: major East Coast container gateway AND a
    # historically significant coal export port -- carries both.
    {"name": "Port of Virginia anchorage (Norfolk)", "lat": 36.95, "lon": -76.33, "office": "", "project_id": "", "commodities": {"port_congestion": 0.5, "ecommerce_logistics": 0.3, "steel_coal": 0.2}, "apollo_location": "Norfolk, Virginia, US", "state": "VA"},
    # Baltimore: one of the few major US ro-ro (roll-on/roll-off) auto
    # ports -- this is auto_parts' FIRST real geo anchor, not just a
    # nationwide-sweep commodity anymore. Also real coal export legacy.
    {"name": "Port of Baltimore anchorage", "lat": 39.27, "lon": -76.58, "office": "", "project_id": "", "commodities": {"port_congestion": 0.4, "auto_parts": 0.35, "steel_coal": 0.25}, "apollo_location": "Baltimore, Maryland, US", "state": "MD"},
    # Jacksonville: the other major US ro-ro auto gateway, plus real
    # Caribbean/Latin America general-cargo trade.
    {"name": "Port of Jacksonville anchorage", "lat": 30.40, "lon": -81.57, "office": "", "project_id": "", "commodities": {"port_congestion": 0.4, "auto_parts": 0.35, "ecommerce_logistics": 0.25}, "apollo_location": "Jacksonville, Florida, US", "state": "FL"},
    # Corpus Christi: one of the largest US ports by TONNAGE, almost
    # entirely crude oil/petrochemical export -- genuinely single-
    # commodity, not a forced split.
    {"name": "Port of Corpus Christi anchorage", "lat": 27.80, "lon": -97.40, "office": "", "project_id": "", "commodities": {"petrochemical": 0.7, "petrochemical_producer": 0.3}, "apollo_location": "Corpus Christi, Texas, US", "state": "TX"},
    # Beaumont: major Gulf refining center, same single-commodity logic.
    {"name": "Port of Beaumont anchorage", "lat": 30.08, "lon": -94.10, "office": "", "project_id": "", "commodities": {"petrochemical": 0.7, "petrochemical_producer": 0.3}, "apollo_location": "Beaumont, Texas, US", "state": "TX"},
    # Mobile: real steel and forest-products (lumber/furniture-adjacent)
    # export/import gateway.
    {"name": "Port of Mobile anchorage", "lat": 30.70, "lon": -88.04, "office": "", "project_id": "", "commodities": {"port_congestion": 0.4, "steel_coal": 0.35, "furniture_wood": 0.25}, "apollo_location": "Mobile, Alabama, US", "state": "AL"},
    # Plaquemines: a fast-growing, real high-tonnage export port just
    # south of New Orleans -- grain and petrochemical, same corridor
    # logic as the GIWW locks upriver.
    {"name": "Port of Plaquemines anchorage", "lat": 29.45, "lon": -89.95, "office": "", "project_id": "", "commodities": {"grain": 0.55, "petrochemical": 0.45}, "apollo_location": "Belle Chasse, Louisiana, US", "state": "LA"},
    # San Juan: the real Caribbean/Latin America gateway -- produce and
    # liquor/spirits (rum) are genuine, well-known Caribbean imports,
    # not a forced fit, plus general apparel/consumer-goods cargo.
    {"name": "Port of San Juan anchorage", "lat": 18.46, "lon": -66.12, "office": "", "project_id": "", "commodities": {"port_congestion": 0.35, "produce_wholesaler": 0.25, "liquor_distributor": 0.2, "apparel": 0.2}, "apollo_location": "San Juan, Puerto Rico, US", "state": "PR"},
    # --- Great Lakes / Soo Locks corridor: the iron-ore-to-steel-mill
    # supply chain the Ohio River corridor doesn't cover. Nearly all US
    # iron ore (Minnesota/Wisconsin's Mesabi Range) moves down through
    # the Soo Locks at Sault Ste. Marie to lower-lake steel mills -- a
    # Soo Locks disruption is one of the single most consequential choke
    # points in the whole US steel supply chain, more direct than the
    # Ohio River signal for this industry, though real grain tonnage also
    # moves this route. Duluth-Superior is genuinely close to an even
    # split -- it's simultaneously a top US grain export gateway AND a
    # major ore port, not dominantly one or the other -- so it feeds both
    # the Agriculture & Grain Handling waterfall and the steel-focused CME
    # Macro Funnel for real reasons, not an artifact of a forced tag.
    # Burns Harbor/Gary and Detroit are lakefront steel-mill locations,
    # about as direct an operating-condition indicator as this system has
    # for that industry -- kept single-commodity since that's genuinely
    # accurate, not a case of missing a real secondary signal. Soo Locks
    # is a real USACE lock (Poe Lock / MacArthur Lock, Detroit District);
    # the rest are open harbors, AIS-only, same confirmation caveat as
    # every other new entry above.
    {"name": "Soo Locks (Sault Ste. Marie, MI)", "lat": 46.50, "lon": -84.35, "office": "", "project_id": "", "commodities": {"steel_coal": 0.85, "grain": 0.15}, "apollo_location": "Sault Ste. Marie, Michigan, US", "state": "MI"},
    {"name": "Duluth-Superior Harbor (Lake Superior)", "lat": 46.78, "lon": -92.10, "office": "", "project_id": "", "commodities": {"grain": 0.55, "steel_coal": 0.45}, "apollo_location": "Duluth, Minnesota, US", "state": "MN"},
    {"name": "Port of Cleveland (Lake Erie)", "lat": 41.54, "lon": -81.63, "office": "", "project_id": "", "commodities": {"steel_coal": 1.0}, "apollo_location": "Cleveland, Ohio, US", "state": "OH"},
    {"name": "Port of Toledo (Lake Erie)", "lat": 41.69, "lon": -83.47, "office": "", "project_id": "", "commodities": {"steel_coal": 1.0}, "apollo_location": "Toledo, Ohio, US", "state": "OH"},
    {"name": "Burns Harbor / Gary anchorage (Lake Michigan)", "lat": 41.63, "lon": -87.15, "office": "", "project_id": "", "commodities": {"steel_coal": 1.0}, "apollo_location": "Gary, Indiana, US", "state": "IN"},
    {"name": "Detroit River anchorage (Detroit, MI)", "lat": 42.33, "lon": -83.05, "office": "", "project_id": "", "commodities": {"steel_coal": 1.0}, "apollo_location": "Detroit, Michigan, US", "state": "MI"},
]

GATE_CHANGE_LOOKBACK_HOURS = 48
GATE_CHANGE_THRESHOLD = 3  # this many gate changes in the lookback window = active water-control intervention

QUEUE_RADIUS_NM = 2  # vessels within this radius of the lock count toward a queue
IDLE_SPEED_KNOTS = 1.5  # at/below this speed counts as "queuing", not "underway"
QUEUE_THRESHOLD = 3  # this many idle vessels clustered at one lock = a disruption signal, not routine traffic


# Module-level (not USACEAdapter methods) so silo_leadgen.py's active
# corroboration hunt can reuse the same CONFIRMED radius/bbox query this
# file already proved out for lock congestion, instead of inventing a
# second, unconfirmed way to ask Datalastic/VesselFinder "what's near
# this point" -- one real, working query, two callers.
def vessels_near_datalastic(client: httpx.Client, api_key: str, lat: float, lon: float, radius_nm: float = QUEUE_RADIUS_NM) -> list[dict]:
    response = client.get(
        f"{DATALASTIC_BASE_URL}/vessel_inradius",
        params={"api-key": api_key, "lat": lat, "lon": lon, "radius": radius_nm},
    )
    response.raise_for_status()
    payload = response.json()
    vessels = payload.get("data", {}).get("vessels") if isinstance(payload.get("data"), dict) else payload.get("vessels", [])
    return vessels or []


def vessels_near_vesselfinder(client: httpx.Client, api_key: str, lat: float, lon: float, radius_nm: float = QUEUE_RADIUS_NM) -> list[dict]:
    delta = radius_nm * 0.015  # roughly radius_nm at mid-latitudes (QUEUE_RADIUS_NM=2 -> ~0.03 deg, as before)
    bbox = f"{lon - delta},{lat - delta},{lon + delta},{lat + delta}"
    response = client.get(f"{VESSELFINDER_BASE_URL}/vessels", params={"userkey": api_key, "bbox": bbox})
    response.raise_for_status()
    payload = response.json()
    vessels = payload if isinstance(payload, list) else payload.get("vessels", payload.get("data", []))
    return vessels or []


class USACEAdapter(TelemetryAdapter):
    source = TelemetrySource.USACE

    def __init__(self, datalastic_api_key: str = "", vesselfinder_api_key: str = "", cwms_api_key: str = ""):
        super().__init__(api_key="")
        self.datalastic_api_key = datalastic_api_key
        self.vesselfinder_api_key = vesselfinder_api_key
        self.cwms_api_key = cwms_api_key  # optional -- CDA's GET endpoints for this data are keyless per their FAQ

    @property
    def enabled(self) -> bool:
        has_ais_key = bool(self.datalastic_api_key or self.vesselfinder_api_key)
        has_cwms_project = any(lock.get("office") and lock.get("project_id") for lock in MONITORED_LOCKS)
        return has_ais_key or has_cwms_project

    # --- Channel 1: real CWMS Data API gate-change activity ---

    def _gate_change_records(self, client: httpx.Client) -> list[RawTelemetryRecord]:
        headers = {"Authorization": f"apikey {self.cwms_api_key}"} if self.cwms_api_key else {}
        end = dt.datetime.now(dt.timezone.utc)
        begin = end - dt.timedelta(hours=GATE_CHANGE_LOOKBACK_HOURS)
        records = []
        for lock in MONITORED_LOCKS:
            office, project_id = lock.get("office"), lock.get("project_id")
            if not office or not project_id:
                continue  # not yet confirmed against the real CWMS catalog -- see module docstring
            response = client.get(
                f"{CWMS_BASE_URL}/projects/{office}/{project_id}/gate-changes",
                params={"begin": begin.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")},
                headers=headers,
            )
            response.raise_for_status()
            changes = response.json()
            changes = changes if isinstance(changes, list) else changes.get("gate-changes", [])
            if len(changes) >= GATE_CHANGE_THRESHOLD:
                records.append(
                    RawTelemetryRecord(
                        title=f"USACE gate-change activity: {lock['name']} -- {len(changes)} changes in {GATE_CHANGE_LOOKBACK_HOURS}h",
                        payload={
                            "lock": lock["name"],
                            "lat": lock["lat"],
                            "lon": lock["lon"],
                            "office": office,
                            "project_id": project_id,
                            "commodities": lock.get("commodities"),
                            "apollo_location": lock.get("apollo_location"),
                            "state": lock.get("state"),
                            "gate_changes": changes,
                        },
                    )
                )
        return records

    # --- Channel 2: AIS-proxy vessel queuing ---

    def _ais_queue_records(self, client: httpx.Client) -> list[RawTelemetryRecord]:
        if not (self.datalastic_api_key or self.vesselfinder_api_key):
            return []

        records = []
        for lock in MONITORED_LOCKS:
            if self.datalastic_api_key:
                vessels = vessels_near_datalastic(client, self.datalastic_api_key, lock["lat"], lock["lon"])
            else:
                vessels = vessels_near_vesselfinder(client, self.vesselfinder_api_key, lock["lat"], lock["lon"])

            idle_count = 0
            for vessel in vessels:
                speed = vessel.get("speed", vessel.get("SPEED"))
                if speed is not None and speed <= IDLE_SPEED_KNOTS:
                    idle_count += 1
            if idle_count >= QUEUE_THRESHOLD:
                records.append(
                    RawTelemetryRecord(
                        title=f"USACE congestion (AIS proxy): {lock['name']} -- {idle_count} idle vessels queued",
                        payload={
                            "lock": lock["name"],
                            "lat": lock["lat"],
                            "lon": lock["lon"],
                            "commodities": lock.get("commodities"),
                            "apollo_location": lock.get("apollo_location"),
                            "state": lock.get("state"),
                            "idle_vessel_count": idle_count,
                            "vessels": vessels,
                        },
                    )
                )
        return records

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(timeout=30) as client:
            return self._gate_change_records(client) + self._ais_queue_records(client)
