"""Regrid parcel/land-lot data adapter.

Regrid's Parcel API (https://regrid.com/api) takes a token query param and
returns parcel records by point/polygon/address query. Fill in the actual
query targets (e.g. parcels near Controlled-Environment Ag or Heavy
Industrial Manufacturing sites) before enabling.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://app.regrid.com/api/v2"


class RegridAdapter(TelemetryAdapter):
    source = TelemetrySource.REGRID

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            # TODO: replace with the actual parcel query (point/polygon/address)
            # for the target sites being surveilled, per Regrid's Parcel API docs.
            response = client.get("/parcels/query", params={"token": self.api_key})
            response.raise_for_status()
            payload = response.json()

        return [
            RawTelemetryRecord(
                title=item.get("properties", {}).get("address", "Parcel record"),
                payload=item,
            )
            for item in payload.get("parcels", {}).get("features", [])
        ]
