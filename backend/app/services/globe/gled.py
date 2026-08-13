"""GLED globe-signal adapter -- NAME/PROVIDER UNCONFIRMED.

"GLED" wasn't a data source I could confidently identify. If you meant
GDELT (the Global Database of Events, Language, and Tone -- a public,
free geolocated world-events feed at https://api.gdeltproject.org, a
natural pairing with GFW 4Wings on a surveillance globe), this stub is
already shaped for it: GDELT's GEO 2.0 API returns per-event lat/lon and
needs no API key. If you meant something else (an internal tool, a
different vendor), tell me the real name/docs and I'll rewrite this
against the actual contract instead of guessing further.

This adapter is disabled until GLED_API_KEY is set OR you confirm GDELT
(which needs no key -- flip `enabled` below once confirmed).
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.globe.base import GlobeAdapter, RawGlobeSignal

GDELT_GEO_URL = "https://api.gdeltproject.org/api/v2/geo/geo"


class GLEDAdapter(GlobeAdapter):
    source = TelemetrySource.GLED

    def fetch_signals(self) -> list[RawGlobeSignal]:
        # TODO: confirm provider identity (see module docstring) before
        # relying on this in production -- currently queries GDELT's free
        # GEO API as a best-guess placeholder for "GLED".
        with httpx.Client(timeout=30) as client:
            response = client.get(GDELT_GEO_URL, params={"query": "logistics OR manufacturing OR agriculture", "format": "geojson"})
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
                    title=props.get("name", "GLED/GDELT event"),
                    payload=props,
                )
            )
        return signals
