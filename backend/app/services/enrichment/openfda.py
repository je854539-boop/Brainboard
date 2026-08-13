"""openFDA compliance/recall-check adapter.

openFDA (https://open.fda.gov) is a public API -- no key required at low
volume, though setting `OPENFDA_API_KEY` raises the rate limit. Checks the
drug/device/food enforcement (recall) endpoints for the business name,
which matters most for Healthcare & Pharma Silo leads but costs nothing to
run on any lead.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://api.fda.gov"


class OpenFDAAdapter(EnrichmentAdapter):
    source = TelemetrySource.OPENFDA

    @property
    def enabled(self) -> bool:
        return True  # public API; api_key (if set) only raises the rate limit

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.business_name:
            raise ValueError("openFDA enforcement lookup requires business_name")

        params = {"search": f'recalling_firm:"{query.business_name}"', "limit": 10}
        if self.api_key:
            params["api_key"] = self.api_key

        results = {}
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            for endpoint in ("drug/enforcement.json", "device/enforcement.json", "food/enforcement.json"):
                response = client.get(f"/{endpoint}", params=params)
                if response.status_code == 404:
                    results[endpoint] = []  # openFDA 404s when a search matches zero records
                    continue
                response.raise_for_status()
                results[endpoint] = response.json().get("results", [])
        return results
