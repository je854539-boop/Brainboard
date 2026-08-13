"""USACE lock-congestion signal, detected as an AIS proxy.

USACE doesn't have a confirmed public REST API for live lock status/
closures reachable from this environment (see git history on this file --
the first version guessed a placeholder endpoint, which is exactly the
kind of silently-wrong integration this project avoids). Instead of
that, this detects the same underlying event indirectly: a lock closure
or extended delay backs barges up right outside the lock, which shows up
in AIS as a cluster of near-stationary vessels sitting at the lock's
coordinates. That's directly observable through the AIS radius-search
capability already wired up for Datalastic (preferred -- clean
lat/lon/radius query) and VesselFinder (bbox fallback) -- no new
external dependency, no USACE-specific API key required. GFW 4Wings is
deliberately *not* used as a fallback here: its default dataset
("public-global-fishing-effort") is fishing-vessel-specific, so it
wouldn't see grain barges at all -- see app/services/globe/gfw_4wings.py.

Feeds CME Macro Funnel: barge congestion on the Mississippi/Illinois
grain corridor is a leading indicator for CME agricultural futures
(corn, soybeans, wheat) volatility, ahead of the price move itself
showing up on CME Globex.

Inert (enabled=False) unless at least one of DATALASTIC_API_KEY /
VESSELFINDER_API_KEY is configured -- there is no separate USACE API key
for this adapter.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

DATALASTIC_BASE_URL = "https://api.datalastic.com/api/v0"  # placeholder -- confirm against your Datalastic API plan
VESSELFINDER_BASE_URL = "https://api.vesselfinder.com"  # placeholder -- confirm against your VesselFinder API plan

# name, lat, lon -- grain-corridor locks on the Mississippi/Illinois
# Waterway most relevant to Chicago commodities flow. Widen/narrow to
# the corridors relevant to your tracked leads.
MONITORED_LOCKS = [
    {"name": "Lock and Dam 1 (Mississippi, Minneapolis)", "lat": 44.92, "lon": -93.20},
    {"name": "Lock and Dam 19 (Mississippi, Keokuk)", "lat": 40.40, "lon": -91.38},
    {"name": "Melvin Price Locks and Dam (Mississippi, Alton)", "lat": 38.87, "lon": -90.16},
    {"name": "Lockport Lock (Illinois Waterway)", "lat": 41.59, "lon": -88.08},
    {"name": "Marseilles Lock (Illinois Waterway)", "lat": 41.33, "lon": -88.71},
    {"name": "Peoria Lock (Illinois Waterway)", "lat": 40.63, "lon": -89.61},
]

QUEUE_RADIUS_NM = 2  # vessels within this radius of the lock count toward a queue
IDLE_SPEED_KNOTS = 1.5  # at/below this speed counts as "queuing", not "underway"
QUEUE_THRESHOLD = 3  # this many idle vessels clustered at one lock = a disruption signal, not routine traffic


class USACEAdapter(TelemetryAdapter):
    source = TelemetrySource.USACE

    def __init__(self, datalastic_api_key: str = "", vesselfinder_api_key: str = ""):
        super().__init__(api_key="")
        self.datalastic_api_key = datalastic_api_key
        self.vesselfinder_api_key = vesselfinder_api_key

    @property
    def enabled(self) -> bool:
        return bool(self.datalastic_api_key or self.vesselfinder_api_key)

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

    def fetch(self) -> list[RawTelemetryRecord]:
        records = []
        with httpx.Client(timeout=30) as client:
            for lock in MONITORED_LOCKS:
                if self.datalastic_api_key:
                    vessels = self._vessels_near_datalastic(client, lock["lat"], lock["lon"])
                elif self.vesselfinder_api_key:
                    vessels = self._vessels_near_vesselfinder(client, lock["lat"], lock["lon"])
                else:
                    vessels = []

                idle_count = 0
                for vessel in vessels:
                    speed = vessel.get("speed", vessel.get("SPEED"))
                    if speed is not None and speed <= IDLE_SPEED_KNOTS:
                        idle_count += 1

                if idle_count >= QUEUE_THRESHOLD:
                    records.append(
                        RawTelemetryRecord(
                            title=f"USACE lock congestion (AIS proxy): {lock['name']} -- {idle_count} idle vessels queued",
                            payload={
                                "lock": lock["name"],
                                "lat": lock["lat"],
                                "lon": lock["lon"],
                                "idle_vessel_count": idle_count,
                                "vessels": vessels,
                            },
                        )
                    )
        return records
