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

`fetch()` keeps running this fixed NATIONAL query (unscoped to any
place) on its own schedule, unchanged. `search()` is the same call
generalized to take any query string -- this is what lets a LOCAL,
place-scoped query run too (see silo_leadgen.py's
_gdelt_local_distress_signals): national coverage catches a big macro
headline ("steel tariff announced"), but would never catch something
like a factory fire or a labor strike at one specific plant in Gary,
Indiana -- too small to make national news, but exactly the kind of
event that creates an urgent, local capital need. GDELT's query
language is boolean keyword/phrase matching, not a geographic radius
search (there's no lat/lon parameter) -- "local" here means requiring
a real place name as part of the query text alongside distress
language, not a true geo-filter.
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

    def search(self, query: str, timespan: str = "24h", maxrecords: int = 20) -> list[RawTelemetryRecord]:
        """Generalized GDELT DOC query -- caller supplies the full query
        string (e.g. distress language AND a required place name for a
        local search). Raises on an HTTP error rather than swallowing
        it, same posture as Apollo/Cobalt's search() methods; callers
        doing several of these in a loop (the local-news sweep) catch
        around this themselves so one bad call can't drop every other
        location's results.
        """
        with httpx.Client(timeout=30) as client:
            response = client.get(
                GDELT_DOC_URL,
                params={"query": query, "mode": "artlist", "format": "json", "timespan": timespan, "maxrecords": maxrecords},
            )
            response.raise_for_status()
            payload = response.json()

        return [
            RawTelemetryRecord(title=article.get("title", "GDELT article"), payload=article)
            for article in payload.get("articles", [])
        ]

    def fetch(self) -> list[RawTelemetryRecord]:
        return self.search(COMMODITY_SHOCK_QUERY, maxrecords=50)
