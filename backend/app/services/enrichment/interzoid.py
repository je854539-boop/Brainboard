"""Interzoid data-quality/enrichment adapter.

Interzoid (https://interzoid.com) exposes many small single-purpose REST
endpoints (company data lookup, address/phone/email validation, name
matching), each taking the API key as a `license` query param. This wraps
their "Get Company Data" endpoint as the default enrichment call; swap the
path for a different Interzoid product if you want a different signal
(e.g. /getaddressstandardized, /getemailquality).
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://api.interzoid.com"


class InterzoidAdapter(EnrichmentAdapter):
    source = TelemetrySource.INTERZOID

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.business_name:
            raise ValueError("Interzoid company lookup requires business_name")

        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.get(
                "/getcompanydata/v1/companydata",
                params={"license": self.api_key, "company": query.business_name},
            )
            response.raise_for_status()
            return response.json()
