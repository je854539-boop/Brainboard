"""Datalastic AIS vessel-tracking macro telemetry sweep.

Same provider/auth/endpoint caveats as app/services/globe/datalastic.py
(area-radius search, `api-key` query param, response envelope unconfirmed
against live docs) -- this is the macro-silo-sweep side of the same
provider, feeding the Oil & Gas / Refining Silo (see silo_leadgen.py).
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.datalastic.com/api/v0"  # placeholder -- confirm against your Datalastic API plan

DEFAULT_LAT = 29.73  # Houston Ship Channel
DEFAULT_LON = -95.03
DEFAULT_RADIUS_NM = 100


class DatalasticAdapter(TelemetryAdapter):
    source = TelemetrySource.DATALASTIC

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            # TODO: set the actual AOI (lat/lon/radius) for the maritime
            # corridors relevant to tracked leads.
            response = client.get(
                "/vessel_inradius",
                params={"api-key": self.api_key, "lat": DEFAULT_LAT, "lon": DEFAULT_LON, "radius": DEFAULT_RADIUS_NM},
            )
            response.raise_for_status()
            payload = response.json()

        vessels = payload.get("data", {}).get("vessels") if isinstance(payload.get("data"), dict) else payload.get("vessels", [])
        return [
            RawTelemetryRecord(title=f"Vessel (Datalastic AIS): {vessel.get('name', 'unknown')}", payload=vessel)
            for vessel in (vessels or [])
        ]
