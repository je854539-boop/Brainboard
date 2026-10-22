"""Cobalt Intelligence macro telemetry sweep.

Cobalt Intelligence's Secretary of State Business Search API (see
app/services/enrichment/cobalt_intelligence.py) is fundamentally a
per-entity lookup -- there's no "give me everything new" bulk endpoint.
`fetch()` keeps running the original fixed watch-list sweep (unchanged,
still feeds the CME Macro Funnel on its own schedule). `search()` is
the same `/api/search` call generalized to take a state/term at call
time -- this is what lets a disrupted lock/port actively trigger a
Cobalt search for the industry THAT disruption implies, in the real
state it happened in (see services/silo_leadgen.py::
_stage2c_cobalt_industry_hunt), instead of only ever running whatever
was pre-configured in MONITORED_SEARCHES. Empty MONITORED_SEARCHES no
longer means this adapter does nothing -- it just means the FIXED sweep
is inert; the disruption-triggered hunt works independently of it.

Requests `uccData=true` on every search -- per Cobalt's own documented
behavior, a subset of states (reported as 11, not independently
confirmed against their live docs) include UCC filing data in the SAME
SOS lookup response at no extra cost. Harmless to pass in every other
state too; this adapter doesn't assume which 11 and just reads
whatever comes back.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.cobaltintelligence.com"

# [{"state": "IL", "search": "grain"}, ...] -- populate with the
# business-name keywords / states relevant to Chicago commodities leads.
MONITORED_SEARCHES: list[dict] = []


class CobaltIntelligenceAdapter(TelemetryAdapter):
    source = TelemetrySource.COBALT_INTELLIGENCE

    def search(self, state: str, search_term: str) -> list[RawTelemetryRecord]:
        """SOS business search with a caller-supplied state/term -- the
        generalized form of fetch() below. Raises on an HTTP error
        rather than swallowing it, same posture as Apollo's search():
        callers needing fault isolation across several calls (e.g. the
        disruption-triggered hunt) catch around this themselves, so a
        real outage is never indistinguishable from "genuinely found
        nothing in this state."
        """
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.get(
                "/api/search",
                params={"apiKey": self.api_key, "state": state, "search": search_term, "uccData": "true"},
            )
            response.raise_for_status()
            return [
                RawTelemetryRecord(title=f"SOS record ({state}): {item.get('name', search_term)}", payload=item)
                for item in response.json().get("results", [])
            ]

    def fetch(self) -> list[RawTelemetryRecord]:
        if not MONITORED_SEARCHES:
            return []
        records = []
        for watch in MONITORED_SEARCHES:
            records += self.search(watch["state"], watch["search"])
        return records
