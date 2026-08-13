"""Cobalt Intelligence macro telemetry sweep.

Cobalt Intelligence's Secretary of State Business Search API (see
app/services/enrichment/cobalt_intelligence.py) is fundamentally a
per-entity lookup -- there's no "give me everything new" bulk endpoint.
To turn it into a macro sweep for the CME Macro Funnel silo, this loops
the same `/api/search` endpoint over a configured watch-list of
(state, search_term) pairs -- e.g. grain elevators, commodity brokers, or
futures-adjacent business names/SIC-relevant keywords in your states of
operation -- checking each for standing/status changes (suspensions,
dissolutions) that can precede a commodities-market-adjacent business
needing financing. Empty by default (inert) until you populate
MONITORED_SEARCHES, same pattern as UCC_FILINGS/SOS_REGISTRIES.
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

    def fetch(self) -> list[RawTelemetryRecord]:
        if not MONITORED_SEARCHES:
            return []

        records = []
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            for watch in MONITORED_SEARCHES:
                response = client.get(
                    "/api/search",
                    params={"apiKey": self.api_key, "state": watch["state"], "search": watch["search"]},
                )
                response.raise_for_status()
                for item in response.json().get("results", []):
                    records.append(
                        RawTelemetryRecord(title=f"SOS record ({watch['state']}): {item.get('name', watch['search'])}", payload=item)
                    )
        return records
