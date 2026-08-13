"""Import Genius per-lead enrichment.

Searches customs bill-of-lading records for shipments tied to the lead's
business name (as consignee/shipper), surfacing import/export activity a
phone call wouldn't have covered -- volume, trade lanes, counterparties.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://www.importgenius.com/api"  # placeholder -- confirm against your Import Genius API plan


class ImportGeniusEnrichmentAdapter(EnrichmentAdapter):
    source = TelemetrySource.IMPORT_GENIUS

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.business_name:
            raise ValueError("Import Genius lookup requires business_name")

        with httpx.Client(base_url=BASE_URL, headers={"X-Api-Key": self.api_key}, timeout=30) as client:
            response = client.get("/v1/shipments", params={"company": query.business_name})
            response.raise_for_status()
            return response.json()
