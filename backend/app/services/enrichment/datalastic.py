"""Datalastic per-lead enrichment.

Datalastic is fundamentally a vessel-identity API (search by vessel name,
IMO, or MMSI) rather than a company-identity one, so searching by the
lead's business name is a best-effort name match, not a guaranteed
company-to-fleet lookup -- flagged as such in the result payload. Useful
for leads in maritime logistics or oil & gas where the business name
itself is also plausibly a vessel/fleet name (e.g. shipping companies
often name vessels after the company).
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://api.datalastic.com/api/v0"  # placeholder -- confirm against your Datalastic API plan


class DatalasticEnrichmentAdapter(EnrichmentAdapter):
    source = TelemetrySource.DATALASTIC

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.business_name:
            raise ValueError("Datalastic lookup requires business_name")

        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.get("/vessel_pro", params={"api-key": self.api_key, "name": query.business_name})
            response.raise_for_status()
            payload = response.json()

        return {"note": "vessel-name search, not a confirmed company-to-fleet match", "result": payload}
