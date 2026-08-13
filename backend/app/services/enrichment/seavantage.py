"""SeaVantage per-lead enrichment.

Searches vessel/fleet and port-call records associated with the lead's
business name -- fleet activity a phone call wouldn't have covered, for
leads in maritime logistics or oil & gas.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://api.seavantage.com"  # placeholder -- confirm against your SeaVantage API plan


class SeaVantageEnrichmentAdapter(EnrichmentAdapter):
    source = TelemetrySource.SEAVANTAGE

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.business_name:
            raise ValueError("SeaVantage lookup requires business_name")

        with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30) as client:
            response = client.get("/v1/vessels", params={"owner": query.business_name})
            response.raise_for_status()
            return response.json()
