"""Import Genius trade/customs manifest data adapter.

Import Genius exposes a REST API for US customs bill-of-lading data under
a paid subscription. Fill in the exact endpoint + query parameters (HS
codes, consignee/shipper filters relevant to Tariff Silo / Food
Processing / Heavy Machinery targets) per their API documentation once
`IMPORT_GENIUS_API_KEY` is provisioned.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://www.importgenius.com/api"  # placeholder -- confirm against your Import Genius API plan


class ImportGeniusAdapter(TelemetryAdapter):
    source = TelemetrySource.IMPORT_GENIUS

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, headers={"X-Api-Key": self.api_key}, timeout=30) as client:
            # TODO: wire to the actual bill-of-lading search endpoint with
            # the HS-code / consignee filters for the silos being surveilled.
            response = client.get("/v1/shipments")
            response.raise_for_status()
            payload = response.json()

        return [
            RawTelemetryRecord(title=item.get("consignee_name", "Import manifest"), payload=item)
            for item in payload.get("results", [])
        ]
