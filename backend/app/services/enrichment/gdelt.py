"""GDELT per-lead enrichment.

Searches recent global news coverage for mentions of the lead's business
name via GDELT's free, keyless DOC 2.0 API -- disputes, lawsuits,
expansions, press a phone call wouldn't have surfaced.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


class GDELTEnrichmentAdapter(EnrichmentAdapter):
    source = TelemetrySource.GDELT

    @property
    def enabled(self) -> bool:
        return True  # free/keyless, same as the globe adapter

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.business_name:
            raise ValueError("GDELT news-mention lookup requires business_name")

        with httpx.Client(timeout=30) as client:
            response = client.get(
                GDELT_DOC_URL,
                params={"query": f'"{query.business_name}"', "mode": "artlist", "format": "json", "maxrecords": 10},
            )
            response.raise_for_status()
            return response.json()
