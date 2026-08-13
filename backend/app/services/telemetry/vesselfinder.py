"""VesselFinder AIS vessel-tracking macro telemetry sweep.

Same provider/auth/endpoint caveats as
app/services/globe/vesselfinder.py (bounding-box area search, `userkey`
query param, response envelope unconfirmed against live docs) -- this is
the macro-silo-sweep side of the same provider, feeding the Oil & Gas /
Refining Silo (see silo_leadgen.py).
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.vesselfinder.com"  # placeholder -- confirm against your VesselFinder API plan

# west, south, east, north -- a box around the Houston Ship Channel / Gulf approach
DEFAULT_BBOX = "-96.0,28.5,-94.0,30.0"


class VesselFinderAdapter(TelemetryAdapter):
    source = TelemetrySource.VESSELFINDER

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            # TODO: set the actual bounding box for the maritime corridors
            # relevant to tracked leads.
            response = client.get("/vessels", params={"userkey": self.api_key, "bbox": DEFAULT_BBOX})
            response.raise_for_status()
            payload = response.json()

        vessels = payload if isinstance(payload, list) else payload.get("vessels", payload.get("data", []))
        return [
            RawTelemetryRecord(
                title=f"Vessel (VesselFinder AIS): {vessel.get('name', vessel.get('NAME', 'unknown'))}",
                payload=vessel,
            )
            for vessel in (vessels or [])
        ]
