"""CME Globex MDP 3.0 market-context enrichment, via Databento.

Same provider/auth/dataset as the telemetry side
(`app/services/telemetry/cme_globex.py`) -- see that module's docstring
for the verified Databento API contract. CME Globex is commodity futures
price data, not a company database, so there's no "look up this
business" query available here. This attaches the current grain-complex
snapshot to the lead's enrichment record as follow-up context, e.g.
"corn is up 4% this week, worth mentioning on the follow-up call" -- not
a personalized lookup.
"""

import datetime as dt
import json

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery
from app.services.telemetry.cme_globex import DATASET, PRICE_SCALE, SYMBOLS

BASE_URL = "https://hist.databento.com"


class CMEGlobexEnrichmentAdapter(EnrichmentAdapter):
    source = TelemetrySource.CME_GLOBEX

    def enrich(self, query: EnrichmentQuery) -> dict:
        end = dt.date.today()
        start = end - dt.timedelta(days=10)

        with httpx.Client(base_url=BASE_URL, auth=(self.api_key, ""), timeout=30) as client:
            response = client.post(
                "/v0/timeseries.get_range",
                data={
                    "dataset": DATASET,
                    "symbols": ",".join(SYMBOLS),
                    "schema": "ohlcv-1d",
                    "stype_in": "continuous",
                    "stype_out": "instrument_id",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "encoding": "json",
                    "compression": "none",
                },
            )
            response.raise_for_status()
            body = response.text

        bars = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                bar = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "open" not in bar or "close" not in bar:
                continue
            bars.append(
                {
                    **bar,
                    "open": bar["open"] / PRICE_SCALE,
                    "high": bar.get("high", 0) / PRICE_SCALE,
                    "low": bar.get("low", 0) / PRICE_SCALE,
                    "close": bar["close"] / PRICE_SCALE,
                }
            )

        return {
            "note": "market-wide macro context (CBOT grain/oilseed complex), not specific to this business",
            "macro_snapshot": bars[:10],
        }
