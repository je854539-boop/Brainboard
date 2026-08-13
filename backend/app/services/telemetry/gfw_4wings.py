"""GFW 4Wings global AIS macro telemetry sweep.

Same API/auth/dataset-uncertainty caveats as
app/services/globe/gfw_4wings.py -- this is the macro-silo-sweep side of
the same provider, feeding the CME Macro Funnel silo. Bulk grain/oilseed
carriers going idle, rerouting, or clustering at a port is a leading
indicator of a commodities-market supply shock, ahead of the price move
itself.
"""

import datetime as dt

import httpx

from app.config import get_settings
from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://gateway.api.globalfishingwatch.org/v3"


class GFW4WingsAdapter(TelemetryAdapter):
    source = TelemetrySource.GFW_4WINGS

    def fetch(self) -> list[RawTelemetryRecord]:
        dataset = get_settings().gfw_dataset
        end = dt.date.today()
        start = end - dt.timedelta(days=7)

        with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30) as client:
            # TODO: set the actual bounding-box geometry for the grain-
            # export corridors relevant to Chicago commodities flow.
            response = client.post(
                "/4wings/report",
                params={"date-range": f"{start.isoformat()},{end.isoformat()}", "spatial-resolution": "low"},
                json={"dataset": dataset},
            )
            response.raise_for_status()
            payload = response.json()

        records = []
        for entry in payload.get("entries", []):
            for cell in entry if isinstance(entry, list) else []:
                records.append(RawTelemetryRecord(title="Marine traffic anomaly (GFW AIS)", payload=cell))
        return records
