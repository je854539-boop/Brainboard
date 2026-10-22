"""Apollo.io macro telemetry sweep.

Unlike the per-lead enrichment adapter (which matches a named contact),
this uses Apollo's organization search/prospecting surface to sweep for
companies matching industry keywords + location. `fetch()` keeps running
the original fixed Chicago commodities-trading sweep (unchanged, still
feeds that specific use case on its own schedule). `search()` is the
same call generalized to take any keywords/locations at call time --
this is what lets a disrupted lock/port actively trigger an Apollo
search for the industry THAT disruption implies, near where it actually
happened, instead of only ever running the one fixed Chicago query (see
services/silo_leadgen.py::_stage2b_apollo_industry_hunt).

Confirm the exact endpoint/params against your Apollo plan -- org
search has moved across a couple of endpoint names in Apollo's API
history and I don't have a live session to verify the current one.
Location filtering is a NAMED location string (city/state/country) in
Apollo's real API, not an arbitrary lat/lon + radius -- callers pass a
real place name (e.g. "Pittsburgh, Pennsylvania, US"), never raw
coordinates, since Apollo's search doesn't support that.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.apollo.io/api/v1"  # placeholder -- confirm against your Apollo.io API plan

# Widen/narrow to the commodities-adjacent industries and metro relevant
# to the fixed Chicago sweep below -- the disruption-triggered hunt in
# silo_leadgen.py passes its own keywords/locations instead of these.
SEARCH_KEYWORDS = ["commodities trading", "grain elevator", "futures brokerage", "agricultural exporter"]
SEARCH_LOCATIONS = ["Chicago, Illinois, US"]


class ApolloAdapter(TelemetryAdapter):
    source = TelemetrySource.APOLLO

    def search(self, keywords: list[str], locations: list[str], per_page: int = 25) -> list[RawTelemetryRecord]:
        """Org search with caller-supplied keywords/locations -- the
        generalized form of fetch() below. Raises on an HTTP error rather
        than swallowing it, same as fetch() always did; callers that need
        "one bad call shouldn't break the rest of a sweep" (e.g. the
        disruption-triggered hunt, which calls this once per distinct
        location) catch around this themselves rather than this method
        silently returning an empty list, so a real outage is never
        indistinguishable from "Apollo genuinely found nothing here."
        """
        with httpx.Client(base_url=BASE_URL, headers={"X-Api-Key": self.api_key}, timeout=30) as client:
            # TODO: confirm this is still Apollo's current org-search
            # endpoint/param names against your API plan's docs.
            response = client.post(
                "/mixed_companies/search",
                json={"q_organization_keyword_tags": keywords, "organization_locations": locations, "per_page": per_page},
            )
            response.raise_for_status()
            payload = response.json()

        return [
            RawTelemetryRecord(title=f"Apollo.io org match: {org.get('name', 'unknown')}", payload=org)
            for org in payload.get("organizations", [])
        ]

    def fetch(self) -> list[RawTelemetryRecord]:
        return self.search(SEARCH_KEYWORDS, SEARCH_LOCATIONS)
