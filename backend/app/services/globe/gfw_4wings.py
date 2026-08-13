"""Global Fishing Watch 4Wings adapter.

GFW's 4Wings API (https://globalfishingwatch.org/our-apis/) reports
gridded vessel-activity ("apparent fishing effort" / AIS presence) for a
bounding box and date range, via
`POST https://gateway.api.globalfishingwatch.org/v3/4wings/report`
with a Bearer API token. Each returned grid cell becomes one globe ping.
Set the bbox/date-range/dataset for your actual surveillance area (Oil &
Gas / logistics maritime corridors) before enabling.
"""

import datetime as dt

import httpx

from app.models.enums import TelemetrySource
from app.services.globe.base import GlobeAdapter, RawGlobeSignal

BASE_URL = "https://gateway.api.globalfishingwatch.org/v3"
DEFAULT_DATASET = "public-global-fishing-effort:latest"


class GFW4WingsAdapter(GlobeAdapter):
    source = TelemetrySource.GFW_4WINGS

    def fetch_signals(self) -> list[RawGlobeSignal]:
        end = dt.date.today()
        start = end - dt.timedelta(days=7)

        with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30) as client:
            # TODO: set the actual bounding-box geometry for the
            # maritime corridors relevant to your logistics/oil & gas leads.
            response = client.post(
                "/4wings/report",
                params={"date-range": f"{start.isoformat()},{end.isoformat()}", "spatial-resolution": "low"},
                json={"dataset": DEFAULT_DATASET},
            )
            response.raise_for_status()
            payload = response.json()

        signals = []
        for entry in payload.get("entries", []):
            for cell in entry if isinstance(entry, list) else []:
                lat, lon = cell.get("lat"), cell.get("lon")
                if lat is None or lon is None:
                    continue
                signals.append(
                    RawGlobeSignal(
                        latitude=lat,
                        longitude=lon,
                        title="GFW 4Wings vessel presence",
                        intensity=cell.get("value"),
                        payload=cell,
                    )
                )
        return signals
