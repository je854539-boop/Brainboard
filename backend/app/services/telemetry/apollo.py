"""Apollo.io macro telemetry sweep.

Unlike the per-lead enrichment adapter (which matches a named contact),
this uses Apollo's organization search/prospecting surface to sweep for
companies matching commodities-trading-relevant criteria (industry
keywords + location), feeding the CME Macro Funnel silo with newly
surfaced grain/futures/commodities-adjacent businesses in the Chicago
market. Confirm the exact endpoint/params against your Apollo plan --
org search has moved across a couple of endpoint names in Apollo's API
history and I don't have a live session to verify the current one.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.apollo.io/api/v1"  # placeholder -- confirm against your Apollo.io API plan

# Widen/narrow to the commodities-adjacent industries and metro relevant
# to your CME Macro Funnel leads.
SEARCH_KEYWORDS = ["commodities trading", "grain elevator", "futures brokerage", "agricultural exporter"]
SEARCH_LOCATIONS = ["Chicago, Illinois, US"]


class ApolloAdapter(TelemetryAdapter):
    source = TelemetrySource.APOLLO

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, headers={"X-Api-Key": self.api_key}, timeout=30) as client:
            # TODO: confirm this is still Apollo's current org-search
            # endpoint/param names against your API plan's docs.
            response = client.post(
                "/mixed_companies/search",
                json={"q_organization_keyword_tags": SEARCH_KEYWORDS, "organization_locations": SEARCH_LOCATIONS, "per_page": 25},
            )
            response.raise_for_status()
            payload = response.json()

        return [
            RawTelemetryRecord(title=f"Apollo.io org match: {org.get('name', 'unknown')}", payload=org)
            for org in payload.get("organizations", [])
        ]
