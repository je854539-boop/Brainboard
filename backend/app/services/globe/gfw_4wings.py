"""Global Fishing Watch 4Wings adapter -- marine traffic layer.

GFW's 4Wings API (https://globalfishingwatch.org/our-apis/) reports
gridded AIS-derived vessel activity for a bounding box and date range, via
`POST https://gateway.api.globalfishingwatch.org/v3/4wings/report` with a
Bearer API token. Each returned grid cell becomes one globe ping.

Which vessels show up is entirely a function of `dataset` below, not
anything this code filters:

  - "public-global-fishing-effort:latest" (the default) is GFW's
    best-known public dataset, but it's fishing-vessel-specific.
  - GFW's broader ocean-transparency product line also covers non-fishing
    AIS traffic (carriers, tankers, etc.) and SAR-based "dark vessel"
    detection, but I don't have high enough confidence in the exact
    dataset ID for that from memory to hardcode it without risking a
    silent wrong-ID failure. Check your GFW API plan/docs for the dataset
    ID that matches your access tier, then set GFW_DATASET in .env --
    no code change needed, this adapter reads it straight from settings.

Set the bbox/date-range for your actual surveillance area (Oil & Gas /
logistics maritime corridors) before enabling -- it currently pulls a
global low-resolution sweep over the last 7 days.
"""

import datetime as dt

import httpx

from app.config import get_settings
from app.models.enums import TelemetrySource
from app.services.globe.base import GlobeAdapter, RawGlobeSignal

BASE_URL = "https://gateway.api.globalfishingwatch.org/v3"


class GFW4WingsAdapter(GlobeAdapter):
    source = TelemetrySource.GFW_4WINGS

    def fetch_signals(self) -> list[RawGlobeSignal]:
        dataset = get_settings().gfw_dataset
        end = dt.date.today()
        start = end - dt.timedelta(days=7)

        with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30) as client:
            # TODO: set the actual bounding-box geometry for the
            # maritime corridors relevant to your logistics/oil & gas leads.
            response = client.post(
                "/4wings/report",
                params={"date-range": f"{start.isoformat()},{end.isoformat()}", "spatial-resolution": "low"},
                json={"dataset": dataset},
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
                        title="Marine traffic (GFW AIS)",
                        intensity=cell.get("value"),
                        payload=cell,
                    )
                )
        return signals
