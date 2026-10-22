"""GDELT commodity/supply-chain-disruption + tariff-news macro telemetry
sweep.

Same free/keyless DOC 2.0 API as the globe and enrichment GDELT adapters,
but queried for commodity-market, supply-chain-shock, AND tariff/trade-
policy news coverage in general (not scoped to one lead or one lat/lon
region) -- feeds the CME Macro Funnel silo as an early-warning signal:
conflict, sanctions, extreme weather, logistics-chokepoint disruptions,
and tariff/trade-policy announcements (Section 301, Section 232, trade
wars, import duties) all move commodity futures and squeeze
import-exposed businesses before the effect is visible anywhere else --
a new tariff announcement is as real a leading indicator as a price
shock, and this is a free source already wired up to catch it.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# OR'd together; narrow/widen to taste. Tuned for commodity-market shock
# AND tariff/trade-policy coverage, rather than the globe adapter's
# general armed-conflict query.
COMMODITY_SHOCK_QUERY = (
    '"commodity market" OR "commodities crash" OR "grain shortage" OR "supply chain disruption" '
    'OR "futures market" OR "export ban" OR "tariff" OR "trade war" OR "import duty" '
    'OR "Section 301" OR "Section 232" OR "steel tariff" OR theme:ECON_COMMODITY OR theme:ECON_STOCKMARKET '
    'OR theme:TAX_FNCACT_TARIFF OR theme:ECON_TRADE_DISPUTE'
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
