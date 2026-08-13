"""GDELT conflict-zone monitoring adapter.

Confirmed provider (previously referenced by the working codename "GLED"
while unconfirmed). Queries GDELT's free, keyless GEO 2.0 API
(https://api.gdeltproject.org/api/v2/geo/geo) for recent geolocated news
coverage tagged with conflict-related GKG themes, returning one globe
signal per located event -- this is what plots conflict zones on the
Globe section.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.globe.base import GlobeAdapter, RawGlobeSignal

GDELT_GEO_URL = "https://api.gdeltproject.org/api/v2/geo/geo"

# GDELT GKG theme codes used to identify conflict-zone coverage. OR'd
# together in the query; narrow this (e.g. drop to just "theme:ARMEDCONFLICT")
# or widen it (add "theme:PROTEST" for civil unrest) to taste.
CONFLICT_QUERY = "theme:ARMEDCONFLICT OR theme:MILITARY OR theme:TERROR"


class GDELTAdapter(GlobeAdapter):
    source = TelemetrySource.GDELT

    @property
    def enabled(self) -> bool:
        return True  # GDELT's GEO 2.0 API is free/keyless; GDELT_API_KEY is unused today

    def fetch_signals(self) -> list[RawGlobeSignal]:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                GDELT_GEO_URL,
                params={
                    "query": CONFLICT_QUERY,
                    "format": "geojson",
                    "mode": "PointData",
                    "timespan": "24h",
                },
            )
            response.raise_for_status()
            payload = response.json()

        signals = []
        for feature in payload.get("features", []):
            coords = feature.get("geometry", {}).get("coordinates")
            if not coords or len(coords) < 2:
                continue
            lon, lat = coords[0], coords[1]
            props = feature.get("properties", {})
            signals.append(
                RawGlobeSignal(
                    latitude=lat,
                    longitude=lon,
                    title=props.get("name") or "GDELT conflict-zone event",
                    payload=props,
                )
            )
        return signals
