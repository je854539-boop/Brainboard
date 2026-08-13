"""Datalastic AIS vessel-tracking adapter -- marine traffic layer.

Datalastic (https://datalastic.com) exposes vessel-position lookups over
a simple REST API authenticated via an `api-key` query parameter (not a
Bearer header). The area-search endpoint used here, `vessel_inradius`,
returns every AIS-reporting vessel within a radius (nautical miles) of a
lat/lon center point.

I don't have a live API session to confirm the exact response envelope
against current docs, so the parsing below is written defensively (it
tolerates a couple of plausible shapes) -- confirm the endpoint path,
param names, and response envelope against your actual Datalastic API
plan before relying on this in production, same caveat as the SeaVantage
and GFW 4Wings adapters carry.

Default AOI is the Houston Ship Channel / Gulf of Mexico approach, since
this feeds the Oil & Gas / Refining Silo (see silo_leadgen.py) -- change
DEFAULT_LAT/LON/RADIUS_NM to whatever maritime corridor is actually
relevant to your leads.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.globe.base import GlobeAdapter, RawGlobeSignal

BASE_URL = "https://api.datalastic.com/api/v0"  # placeholder -- confirm against your Datalastic API plan

DEFAULT_LAT = 29.73  # Houston Ship Channel
DEFAULT_LON = -95.03
DEFAULT_RADIUS_NM = 100


class DatalasticAdapter(GlobeAdapter):
    source = TelemetrySource.DATALASTIC

    def fetch_signals(self) -> list[RawGlobeSignal]:
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            # TODO: set the actual AOI (lat/lon/radius) for the maritime
            # corridors relevant to your logistics/oil & gas leads.
            response = client.get(
                "/vessel_inradius",
                params={"api-key": self.api_key, "lat": DEFAULT_LAT, "lon": DEFAULT_LON, "radius": DEFAULT_RADIUS_NM},
            )
            response.raise_for_status()
            payload = response.json()

        vessels = payload.get("data", {}).get("vessels") if isinstance(payload.get("data"), dict) else payload.get("vessels", [])
        signals = []
        for vessel in vessels or []:
            lat, lon = vessel.get("lat"), vessel.get("lon")
            if lat is None or lon is None:
                continue
            signals.append(
                RawGlobeSignal(
                    latitude=lat,
                    longitude=lon,
                    title=f"Vessel (Datalastic AIS): {vessel.get('name', 'unknown')}",
                    payload=vessel,
                )
            )
        return signals
