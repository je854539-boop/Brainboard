"""HigherGov per-lead enrichment.

Searches federal contract/award history for the lead's business name --
government-contractor revenue a phone call likely didn't surface.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://www.highergov.com/api-external"


class HigherGovEnrichmentAdapter(EnrichmentAdapter):
    source = TelemetrySource.HIGHERGOV

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.business_name:
            raise ValueError("HigherGov lookup requires business_name")

        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.get("/awards/", params={"api_key": self.api_key, "recipient_name": query.business_name})
            response.raise_for_status()
            return response.json()
