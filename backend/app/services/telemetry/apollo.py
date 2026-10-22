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

`search_people()` is the real gap the enrichment adapter's `/people/
match` can't fill: match requires a name or email you ALREADY have, so
it only enriches a contact you've already found. Finding WHO to ask for
at a company you only know by name -- no contact name yet -- needs
Apollo's People Search surface instead (`/mixed_people/search`,
filtered by the organization's Apollo id + a list of titles, e.g.
"Owner"/"CFO"), not the match endpoint. Same endpoint-name-may-have-
moved caveat as search() above.

`enrich_organization()` is the per-company revenue check: given just a
name (no domain, no Apollo id -- all you ever get from a passive feed
like Import Genius), calls the real `/organizations/enrich` endpoint
(same one documented in services/enrichment/apollo.py, used here for a
company-only lookup instead of a contact match) to get back a real
revenue figure for THAT specific company, on demand -- not a passive
scan of whatever Apollo data happened to already be lying around from
an unrelated sweep.
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

    def search_people(self, organization_id: str, titles: list[str], per_page: int = 5) -> list[RawTelemetryRecord]:
        """Finds named people at a KNOWN organization (by Apollo's own
        org id, as returned on every record from search() above) holding
        one of the given titles -- the actual decision-maker-discovery
        call, distinct from the enrichment adapter's /people/match (which
        needs a name/email you already have). Each returned record's
        payload is normalized onto the generic contact-field names
        _extract_contact_fields already checks (see silo_leadgen.py) --
        phone/email -- plus contact_name/contact_title, so this slots
        into the existing contact-enrichment path without that function
        needing to know anything Apollo-specific changed upstream.

        TODO: confirm the exact response field names (first_name/
        last_name/title/email/phone_numbers) against your Apollo plan --
        same unconfirmed-schema posture as search() above.
        """
        with httpx.Client(base_url=BASE_URL, headers={"X-Api-Key": self.api_key}, timeout=30) as client:
            response = client.post(
                "/mixed_people/search",
                json={"organization_ids": [organization_id], "person_titles": titles, "per_page": per_page},
            )
            response.raise_for_status()
            payload = response.json()

        records = []
        for person in payload.get("people", []):
            full_name = " ".join(filter(None, [person.get("first_name"), person.get("last_name")])) or person.get("name") or "unknown"
            phone_numbers = person.get("phone_numbers") or []
            phone = phone_numbers[0].get("raw_number") if phone_numbers and isinstance(phone_numbers[0], dict) else None
            records.append(
                RawTelemetryRecord(
                    title=f"Apollo.io person match: {full_name} ({person.get('title', 'unknown title')})",
                    payload={
                        "contact_name": full_name,
                        "contact_title": person.get("title"),
                        "phone": phone,
                        "email": person.get("email"),
                        **person,
                    },
                )
            )
        return records

    def enrich_organization(self, name: str) -> dict | None:
        """Real-time, per-company lookup by name alone -- the active
        check Import Genius's passive consignee-name feed needs to find
        out whether a specific company is actually "big ticket" (in the
        target revenue band) instead of waiting for a name match
        against whatever Apollo data happens to already be sitting in
        the database from an unrelated sweep. Returns the raw org
        payload (expected to carry a revenue-ish field somewhere,
        checked defensively downstream) or None on no match / HTTP
        failure -- callers must not treat None as "confirmed small,"
        only as "no data yet," same honesty posture as every other
        revenue check in this project.
        """
        with httpx.Client(base_url=BASE_URL, headers={"X-Api-Key": self.api_key}, timeout=30) as client:
            try:
                response = client.post("/organizations/enrich", json={"organization_name": name})
                response.raise_for_status()
            except httpx.HTTPError:
                return None
            payload = response.json()
        return payload.get("organization") or payload or None
