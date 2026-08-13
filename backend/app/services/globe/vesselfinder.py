"""VesselFinder AIS vessel-tracking adapter -- marine traffic layer.

VesselFinder (https://www.vesselfinder.com) exposes an AIS vessel-position
API authenticated via a `userkey` query parameter. Historically their
base API tier is scoped per-vessel/per-fleet (MMSI/IMO lookups) rather
than an open bounding-box area query -- an area/polygon sweep like the
one below may require their PRO tier. I don't have a live API session to
confirm the exact endpoint path or param names against current docs, so
confirm both against your actual VesselFinder API plan before relying on
this in production, same caveat as the SeaVantage and GFW 4Wings adapters
carry.

Default AOI is the Houston Ship Channel / Gulf of Mexico approach, since
this feeds the Oil & Gas / Refining Silo (see silo_leadgen.py) -- change
DEFAULT_BBOX to whatever maritime corridor is actually relevant to your
leads.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.globe.base import GlobeAdapter, RawGlobeSignal

BASE_URL = "https://api.vesselfinder.com"  # placeholder -- confirm against your VesselFinder API plan

# west, south, east, north -- a box around the Houston Ship Channel / Gulf approach
DEFAULT_BBOX = "-96.0,28.5,-94.0,30.0"


class VesselFinderAdapter(GlobeAdapter):
    source = TelemetrySource.VESSELFINDER

    def fetch_signals(self) -> list[RawGlobeSignal]:
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            # TODO: set the actual bounding box for the maritime corridors
            # relevant to your logistics/oil & gas leads.
            response = client.get("/vessels", params={"userkey": self.api_key, "bbox": DEFAULT_BBOX})
            response.raise_for_status()
            payload = response.json()

        vessels = payload if isinstance(payload, list) else payload.get("vessels", payload.get("data", []))
        signals = []
        for vessel in vessels or []:
            # Field casing varies across VesselFinder's endpoints/versions
            # (some return upper-case AIS field names) -- check both.
            lat = vessel.get("lat", vessel.get("LAT"))
            lon = vessel.get("lon", vessel.get("LON"))
            if lat is None or lon is None:
                continue
            name = vessel.get("name", vessel.get("NAME", "unknown"))
            signals.append(
                RawGlobeSignal(
                    latitude=lat,
                    longitude=lon,
                    title=f"Vessel (VesselFinder AIS): {name}",
                    payload=vessel,
                )
            )
        return signals
