"""GDELT commodity/supply-chain-disruption macro telemetry sweep.

Same free/keyless DOC 2.0 API as the globe and enrichment GDELT adapters,
but queried for commodity-market and supply-chain-shock news coverage in
general (not scoped to one lead or one lat/lon region) -- feeds the CME
Macro Funnel silo as an early-warning signal for Chicago commodities
(CME) market swings and crashes: conflict, sanctions, extreme weather,
and logistics-chokepoint disruptions all move commodity futures before
the price move itself is visible on CME Globex.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# OR'd together; narrow/widen to taste. Tuned for commodity-market shock
# coverage rather than the globe adapter's general armed-conflict query.
COMMODITY_SHOCK_QUERY = (
    '"commodity market" OR "commodities crash" OR "grain shortage" OR "supply chain disruption" '
    'OR "futures market" OR "export ban" OR theme:ECON_COMMODITY OR theme:ECON_STOCKMARKET'
)


class GDELTAdapter(TelemetryAdapter):
    source = TelemetrySource.GDELT

    @property
    def enabled(self) -> bool:
        return True  # free/keyless, same as the globe and enrichment adapters

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                GDELT_DOC_URL,
                params={"query": COMMODITY_SHOCK_QUERY, "mode": "artlist", "format": "json", "timespan": "24h", "maxrecords": 50},
            )
            response.raise_for_status()
            payload = response.json()

        return [
            RawTelemetryRecord(title=article.get("title", "GDELT commodity-shock article"), payload=article)
            for article in payload.get("articles", [])
        ]
