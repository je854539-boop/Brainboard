"""SeaVantage ocean-traffic / AIS vessel tracking adapter.

SeaVantage's API surfaces vessel position, port-call, and ETA data. Fill
in the exact endpoint and area-of-interest / vessel filters (relevant to
Oil & Gas, Agriculture & Grain Handling, and Heavy Machinery import/export
flows) per their API docs once `SEAVANTAGE_API_KEY` is provisioned.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.seavantage.com"  # placeholder -- confirm against your SeaVantage API plan


class SeaVantageAdapter(TelemetryAdapter):
    source = TelemetrySource.SEAVANTAGE

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30) as client:
            # TODO: wire to the actual vessel-tracking / port-call endpoint
            # with the AOI and vessel-type filters relevant to tracked leads.
            response = client.get("/v1/vessels")
            response.raise_for_status()
            payload = response.json()

        return [
            RawTelemetryRecord(title=item.get("vessel_name", "Vessel telemetry"), payload=item)
            for item in payload.get("vessels", [])
        ]
