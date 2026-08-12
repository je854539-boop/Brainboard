"""CME Globex 3.0 market data adapter.

CME's market data API is a licensed, contract-specific product (Globex
MDP 3.0 / CME DataMine) -- the exact base URL, auth flow, and channel/
instrument codes depend on the specific data license negotiated with CME.
Fill in `BASE_URL` and `fetch()` per your executed CME market data
agreement before enabling. Until `CME_GLOBEX_API_KEY` is set this adapter
stays disabled and `ingest()` is a no-op.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.cmegroup.com"  # placeholder -- replace with your licensed CME DataMine/Globex endpoint


class CMEGlobexAdapter(TelemetryAdapter):
    source = TelemetrySource.CME_GLOBEX

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30) as client:
            # TODO: point at the specific licensed CME Globex/DataMine
            # endpoint + instrument/channel filters relevant to the target
            # commodity/futures complexes tracked by the CME Macro Funnel silo.
            response = client.get("/v1/market-data")
            response.raise_for_status()
            payload = response.json()

        return [
            RawTelemetryRecord(title=item.get("symbol", "CME Globex event"), payload=item)
            for item in payload.get("data", [])
        ]
