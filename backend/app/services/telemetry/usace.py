"""USACE lock-disruption signal, from two complementary channels.

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
# lookup; this channel silently skips any entry left blank).
MONITORED_LOCKS = [
    {"name": "Lock and Dam 1 (Mississippi, Minneapolis)", "lat": 44.92, "lon": -93.20, "office": "", "project_id": ""},
    {"name": "Lock and Dam 19 (Mississippi, Keokuk)", "lat": 40.40, "lon": -91.38, "office": "", "project_id": ""},
    {"name": "Melvin Price Locks and Dam (Mississippi, Alton)", "lat": 38.87, "lon": -90.16, "office": "", "project_id": ""},
    {"name": "Lockport Lock (Illinois Waterway)", "lat": 41.59, "lon": -88.08, "office": "", "project_id": ""},
    {"name": "Marseilles Lock (Illinois Waterway)", "lat": 41.33, "lon": -88.71, "office": "", "project_id": ""},
    {"name": "Peoria Lock (Illinois Waterway)", "lat": 40.63, "lon": -89.61, "office": "", "project_id": ""},
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
                        payload={"lock": lock["name"], "office": office, "project_id": project_id, "gate_changes": changes},
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
                        title=f"USACE lock congestion (AIS proxy): {lock['name']} -- {idle_count} idle vessels queued",
                        payload={"lock": lock["name"], "lat": lock["lat"], "lon": lock["lon"], "idle_vessel_count": idle_count, "vessels": vessels},
                    )
                )
        return records

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(timeout=30) as client:
            return self._gate_change_records(client) + self._ais_queue_records(client)
