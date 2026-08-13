"""Cobalt Intelligence business-verification adapter.

Cobalt Intelligence's Secretary of State Business Search API
(https://cobaltintelligence.com) looks up a business's SOS registration,
officers, and standing by name + state. Confirm the exact request/response
shape against your plan's docs -- this targets their documented
`/api/search` pattern (API key as query param) as of general availability.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://api.cobaltintelligence.com"


class CobaltIntelligenceAdapter(EnrichmentAdapter):
    source = TelemetrySource.COBALT_INTELLIGENCE

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.business_name or not query.state:
            raise ValueError("Cobalt Intelligence lookup requires business_name and state")

        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.get(
                "/api/search",
                params={"apiKey": self.api_key, "state": query.state, "search": query.business_name},
            )
            response.raise_for_status()
            return response.json()
