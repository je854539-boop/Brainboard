"""Regrid per-lead enrichment.

Looks up parcel/property records for the lead's address (falling back to
business name), surfacing ownership, zoning, and lot details a phone call
wouldn't have covered -- useful for collateral/site-visit context.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://app.regrid.com/api/v2"


class RegridEnrichmentAdapter(EnrichmentAdapter):
    source = TelemetrySource.REGRID

    def enrich(self, query: EnrichmentQuery) -> dict:
        target = query.address or query.business_name
        if not target:
            raise ValueError("Regrid lookup requires address or business_name")

        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.get("/parcels/query", params={"token": self.api_key, "query": target})
            response.raise_for_status()
            return response.json()
