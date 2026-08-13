"""VesselFinder per-lead enrichment.

Same caveat as the Datalastic enrichment adapter: VesselFinder is
fundamentally a vessel-identity API (search by vessel name, IMO, or
MMSI), so searching by the lead's business name is a best-effort name
match, not a guaranteed company-to-fleet lookup -- flagged as such in the
result payload.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://api.vesselfinder.com"  # placeholder -- confirm against your VesselFinder API plan


class VesselFinderEnrichmentAdapter(EnrichmentAdapter):
    source = TelemetrySource.VESSELFINDER

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.business_name:
            raise ValueError("VesselFinder lookup requires business_name")

        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.get("/vessels", params={"userkey": self.api_key, "name": query.business_name})
            response.raise_for_status()
            payload = response.json()

        return {"note": "vessel-name search, not a confirmed company-to-fleet match", "result": payload}
