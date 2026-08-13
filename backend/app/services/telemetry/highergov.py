"""HigherGov federal contract/award data adapter.

HigherGov (https://www.highergov.com) exposes a REST API for
opportunity/award/contract search, keyed by API key. Feeds the HigherGov
Funnel silo. Confirm the exact endpoint + search filters (NAICS codes,
place of performance, agency) against your HigherGov API plan.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://www.highergov.com/api-external"


class HigherGovAdapter(TelemetryAdapter):
    source = TelemetrySource.HIGHERGOV

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            # TODO: set NAICS / agency / place-of-performance filters for
            # the target sectors (Heavy Industrial, Aerospace, Ag, etc.)
            response = client.get("/opportunity/", params={"api_key": self.api_key})
            response.raise_for_status()
            payload = response.json()

        return [
            RawTelemetryRecord(title=item.get("title", "HigherGov opportunity"), payload=item)
            for item in payload.get("results", [])
        ]
