"""CME Globex 3.0 market-context enrichment adapter.

CME Globex is commodity/futures market data, not a company database --
there's no "look up this business" query available here. This attaches
the current macro market snapshot (same feed as the CME Globex silo
telemetry sweep, see telemetry/cme_globex.py) to the lead's enrichment
record as follow-up context: e.g. "grain complex volatility is up, worth
mentioning on the follow-up call" -- not a personalized lookup.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://api.cmegroup.com"  # placeholder -- see telemetry/cme_globex.py for the same caveat


class CMEGlobexEnrichmentAdapter(EnrichmentAdapter):
    source = TelemetrySource.CME_GLOBEX

    def enrich(self, query: EnrichmentQuery) -> dict:
        with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30) as client:
            response = client.get("/v1/market-data")
            response.raise_for_status()
            payload = response.json()
        return {
            "note": "market-wide macro context, not specific to this business",
            "macro_snapshot": payload.get("data", [])[:10],
        }
