"""Apollo.io contact/company enrichment adapter.

Apollo's People/Organization Enrichment endpoints
(https://api.apollo.io/api/v1/people/match,
https://api.apollo.io/api/v1/organizations/enrich) take the API key in
the `X-Api-Key` header and match on name/email/domain. This wraps the
person-match endpoint since a lead usually has a named contact; switch to
organizations/enrich for a company-only lookup.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://api.apollo.io/api/v1"


class ApolloAdapter(EnrichmentAdapter):
    source = TelemetrySource.APOLLO

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.contact_name and not query.email:
            raise ValueError("Apollo.io lookup requires contact_name or email")

        payload = {
            "name": query.contact_name,
            "email": query.email,
            "organization_name": query.business_name,
        }
        with httpx.Client(base_url=BASE_URL, headers={"X-Api-Key": self.api_key}, timeout=30) as client:
            response = client.post("/people/match", json={k: v for k, v in payload.items() if v})
            response.raise_for_status()
            return response.json()
