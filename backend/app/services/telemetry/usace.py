"""USACE lock/port-disruption signal, from two complementary channels.

**Multi-corridor, multi-commodity.** MONITORED_LOCKS originally covered
only the Mississippi/Illinois grain corridor (feeding the Agriculture &
Grain Handling silo). It now also covers the Ohio River corridor (coal +
steel -- the Pittsburgh/upper-Ohio steel region moves both by barge),
the Gulf Intracoastal Waterway (petrochemicals, around the Houston/Lake
Charles/Baton Rouge corridor), the Great Lakes/Soo Locks corridor (the
iron-ore-to-steel-mill supply chain, plus Duluth-Superior grain exports),
and four coastal port anchorage points (marine-traffic-at-port
congestion, not river-lock congestion -- see below). Every entry carries a `commodity` tag so a disruption signal
says what it actually means (a steel-corridor disruption is not a grain
signal) -- callers filter `_usace_disruption_signals` by commodity
instead of treating every USACE event as agriculture-specific. Real
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
# lookup; this channel silently skips any entry left blank), commodity
# (what this segment's disruption actually signals -- see module
# docstring; callers filter _usace_disruption_signals by this tag).
MONITORED_LOCKS = [
    # --- Mississippi / Illinois Waterway: grain corridor ---
    {"name": "Lock and Dam 1 (Mississippi, Minneapolis)", "lat": 44.92, "lon": -93.20, "office": "", "project_id": "", "commodity": "grain"},
    {"name": "Lock and Dam 19 (Mississippi, Keokuk)", "lat": 40.40, "lon": -91.38, "office": "", "project_id": "", "commodity": "grain"},
    {"name": "Melvin Price Locks and Dam (Mississippi, Alton)", "lat": 38.87, "lon": -90.16, "office": "", "project_id": "", "commodity": "grain"},
    {"name": "Lockport Lock (Illinois Waterway)", "lat": 41.59, "lon": -88.08, "office": "", "project_id": "", "commodity": "grain"},
    {"name": "Marseilles Lock (Illinois Waterway)", "lat": 41.33, "lon": -88.71, "office": "", "project_id": "", "commodity": "grain"},
    {"name": "Peoria Lock (Illinois Waterway)", "lat": 40.63, "lon": -89.61, "office": "", "project_id": "", "commodity": "grain"},
    # --- Ohio River: coal + steel corridor (Pittsburgh/upper-Ohio steel
    # region down through the coal-heavy middle Ohio) -- lock names and
    # general geography are well-established public infrastructure;
    # office/project_id NOT yet confirmed against the live CWMS catalog,
    # see module docstring.
    {"name": "Emsworth Locks and Dam (Ohio River, Pittsburgh)", "lat": 40.51, "lon": -80.09, "office": "", "project_id": "", "commodity": "steel_coal"},
    {"name": "Montgomery Locks and Dam (Ohio River, Pittsburgh)", "lat": 40.64, "lon": -80.19, "office": "", "project_id": "", "commodity": "steel_coal"},
    {"name": "New Cumberland Locks and Dam (Ohio River, Steubenville/Weirton)", "lat": 40.50, "lon": -80.62, "office": "", "project_id": "", "commodity": "steel_coal"},
    {"name": "Greenup Locks and Dam (Ohio River, Huntington)", "lat": 38.65, "lon": -82.93, "office": "", "project_id": "", "commodity": "steel_coal"},
    {"name": "McAlpine Locks and Dam (Ohio River, Louisville)", "lat": 38.26, "lon": -85.78, "office": "", "project_id": "", "commodity": "steel_coal"},
    {"name": "Olmsted Locks and Dam (Ohio River, mouth)", "lat": 37.18, "lon": -89.07, "office": "", "project_id": "", "commodity": "steel_coal"},
    # --- Gulf Intracoastal Waterway: petrochemical corridor (Houston /
    # Lake Charles / Baton Rouge) -- same confirmation caveat as above.
    {"name": "Algiers Lock (GIWW, New Orleans)", "lat": 29.93, "lon": -90.03, "office": "", "project_id": "", "commodity": "petrochemical"},
    {"name": "Port Allen Lock (GIWW, Baton Rouge)", "lat": 30.45, "lon": -91.21, "office": "", "project_id": "", "commodity": "petrochemical"},
    {"name": "Calcasieu Lock (GIWW, Lake Charles)", "lat": 30.12, "lon": -93.36, "office": "", "project_id": "", "commodity": "petrochemical"},
    # --- Coastal port anchorages: marine-traffic-at-port congestion,
    # AIS-only (no CWMS lock-gate data applies -- office/project_id left
    # blank on purpose, see module docstring). Picked to match your real
    # book's top states (CA/TX/NY/FL/NJ import gateways).
    {"name": "Port of Los Angeles / Long Beach anchorage", "lat": 33.72, "lon": -118.25, "office": "", "project_id": "", "commodity": "port_congestion"},
    {"name": "Houston Ship Channel anchorage", "lat": 29.42, "lon": -94.90, "office": "", "project_id": "", "commodity": "port_congestion"},
    {"name": "Port of New York / New Jersey anchorage", "lat": 40.47, "lon": -73.95, "office": "", "project_id": "", "commodity": "port_congestion"},
    {"name": "Port Everglades / Miami anchorage", "lat": 26.09, "lon": -80.10, "office": "", "project_id": "", "commodity": "port_congestion"},
    # --- Great Lakes / Soo Locks corridor: the iron-ore-to-steel-mill
    # supply chain the Ohio River corridor doesn't cover. Nearly all US
    # iron ore (Minnesota/Wisconsin's Mesabi Range) moves down through
    # the Soo Locks at Sault Ste. Marie to lower-lake steel mills -- a
    # Soo Locks disruption is one of the single most consequential choke
    # points in the whole US steel supply chain, more direct than the
    # Ohio River signal for this industry. Duluth-Superior is tagged
    # "grain" instead -- it's a top US grain export gateway in its own
    # right (Great Lakes/Seaway route), not just an ore port, so it also
    # feeds the Agriculture & Grain Handling waterfall, not only the
    # broader steel-focused CME Macro Funnel. Burns Harbor/Gary and
    # Detroit are lakefront steel-mill locations, not generic ports --
    # about as direct an operating-condition indicator as this system
    # has for that industry. Soo Locks is a real USACE lock (Poe Lock /
    # MacArthur Lock, Detroit District); the rest are open harbors,
    # AIS-only, same confirmation caveat as every other new entry above.
    {"name": "Soo Locks (Sault Ste. Marie, MI)", "lat": 46.50, "lon": -84.35, "office": "", "project_id": "", "commodity": "steel_coal"},
    {"name": "Duluth-Superior Harbor (Lake Superior)", "lat": 46.78, "lon": -92.10, "office": "", "project_id": "", "commodity": "grain"},
    {"name": "Port of Cleveland (Lake Erie)", "lat": 41.54, "lon": -81.63, "office": "", "project_id": "", "commodity": "steel_coal"},
    {"name": "Port of Toledo (Lake Erie)", "lat": 41.69, "lon": -83.47, "office": "", "project_id": "", "commodity": "steel_coal"},
    {"name": "Burns Harbor / Gary anchorage (Lake Michigan)", "lat": 41.63, "lon": -87.15, "office": "", "project_id": "", "commodity": "steel_coal"},
    {"name": "Detroit River anchorage (Detroit, MI)", "lat": 42.33, "lon": -83.05, "office": "", "project_id": "", "commodity": "steel_coal"},
]

GATE_CHANGE_LOOKBACK_HOURS = 48
GATE_CHANGE_THRESHOLD = 3  # this many gate changes in the lookback window = active water-control intervention

QUEUE_RADIUS_NM = 2  # vessels within this radius of the lock count toward a queue
IDLE_SPEED_KNOTS = 1.5  # at/below this speed counts as "queuing", not "underway"
QUEUE_THRESHOLD = 3  # this many idle vessels clustered at one lock = a disruption signal, not routine traffic


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
                            "office": office,
                            "project_id": project_id,
                            "commodity": lock.get("commodity"),
                            "gate_changes": changes,
                        },
                    )
                )
        return records

    # --- Channel 2: AIS-proxy vessel queuing ---

    def _vessels_near_datalastic(self, client: httpx.Client, lat: float, lon: float) -> list[dict]:
        response = client.get(
            f"{DATALASTIC_BASE_URL}/vessel_inradius",
            params={"api-key": self.datalastic_api_key, "lat": lat, "lon": lon, "radius": QUEUE_RADIUS_NM},
        )
        response.raise_for_status()
        payload = response.json()
        vessels = payload.get("data", {}).get("vessels") if isinstance(payload.get("data"), dict) else payload.get("vessels", [])
        return vessels or []

    def _vessels_near_vesselfinder(self, client: httpx.Client, lat: float, lon: float) -> list[dict]:
        delta = 0.03  # roughly QUEUE_RADIUS_NM at mid-latitudes
        bbox = f"{lon - delta},{lat - delta},{lon + delta},{lat + delta}"
        response = client.get(f"{VESSELFINDER_BASE_URL}/vessels", params={"userkey": self.vesselfinder_api_key, "bbox": bbox})
        response.raise_for_status()
        payload = response.json()
        vessels = payload if isinstance(payload, list) else payload.get("vessels", payload.get("data", []))
        return vessels or []

    def _ais_queue_records(self, client: httpx.Client) -> list[RawTelemetryRecord]:
        if not (self.datalastic_api_key or self.vesselfinder_api_key):
            return []

        records = []
        for lock in MONITORED_LOCKS:
            if self.datalastic_api_key:
                vessels = self._vessels_near_datalastic(client, lock["lat"], lock["lon"])
            else:
                vessels = self._vessels_near_vesselfinder(client, lock["lat"], lock["lon"])

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
                            "commodity": lock.get("commodity"),
                            "idle_vessel_count": idle_count,
                            "vessels": vessels,
                        },
                    )
                )
        return records

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(timeout=30) as client:
            return self._gate_change_records(client) + self._ais_queue_records(client)
