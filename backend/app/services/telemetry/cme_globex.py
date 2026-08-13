"""CME Globex MDP 3.0 futures-price telemetry, via Databento.

Raw CME Globex MDP 3.0 access is a direct-exchange feed that requires a
CME market-data license plus colocation/cross-connect infrastructure --
not practical for this deployment. Databento (databento.com) is a
licensed redistributor that resells the same MDP 3.0 data over a plain
HTTPS historical API, which is what this adapter actually calls.

Verified against the real `databento-python` client source (not
guessed): base URL, endpoint, auth scheme, dataset code, and continuous-
contract symbology below all match
https://github.com/databento/databento-python.

- Base URL: `databento/common/enums.py::HistoricalGateway.BO1`
- Endpoint: `POST /v0/timeseries.get_range` (`databento/historical/api/timeseries.py`)
- Auth: HTTP Basic, API key as username, blank password (`databento/common/http.py`)
- Dataset code for CME Globex MDP 3.0: `GLBX.MDP3` (`tests/test_live_gateway_messages.py`)
- Continuous front-month symbology: `ROOT.c.0`, e.g. `ZC.c.0` (`tests/test_common_symbology.py`)
- OHLCV record fields (`open`/`high`/`low`/`close`/`volume`) confirmed in
  `tests/test_historical_bento.py`; prices are fixed-point, scaled 1e-9.

This adapter uses `encoding=json` (newline-delimited JSON records) via a
plain httpx POST rather than the `databento` SDK's binary DBN client, to
avoid pulling in a compiled binary-parsing dependency for a handful of
daily OHLCV bars.
"""

import datetime as dt
import json

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://hist.databento.com"
DATASET = "GLBX.MDP3"  # CME Globex MDP 3.0, redistributed by Databento
PRICE_SCALE = 1_000_000_000  # DBN fixed-point prices are scaled 1e-9

# CBOT grain/oilseed complex continuous front-month contracts -- the core
# "Chicago commodities market" instruments the CME Macro Funnel silo is
# built around (corn, soybeans, wheat, soybean meal, soybean oil).
SYMBOLS = ["ZC.c.0", "ZS.c.0", "ZW.c.0", "ZM.c.0", "ZL.c.0"]


class CMEGlobexAdapter(TelemetryAdapter):
    source = TelemetrySource.CME_GLOBEX

    def fetch(self) -> list[RawTelemetryRecord]:
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

        records = []
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
            symbol = bar.get("symbol") or str(bar.get("instrument_id", "?"))
            normalized = {
                **bar,
                "open": bar["open"] / PRICE_SCALE,
                "high": bar.get("high", 0) / PRICE_SCALE,
                "low": bar.get("low", 0) / PRICE_SCALE,
                "close": bar["close"] / PRICE_SCALE,
            }
            records.append(RawTelemetryRecord(title=f"CME Globex OHLCV: {symbol}", payload=normalized))
        return records
