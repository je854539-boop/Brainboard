"""Rail traffic adapter -- macro congestion/volume context only.

HONESTY NOTE, same posture as the LPMS note in river_surveillance.py:
the AAR (Association of American Railroads) publishes a real weekly
rail traffic report (carload volume by commodity category) and the
Surface Transportation Board requires Class I railroads to publicly
report service-performance metrics (terminal dwell, train speed), but
both are published as downloadable reports, not a confirmed clean REST
API the way api.weather.gov or CWMS are. This adapter is real
scaffolding -- the shape every other adapter in this project follows --
written against a plausible access pattern (a scheduled pull of a
published report endpoint), but it stays OFF (`enabled` False) until a
real access method is actually confirmed and wired in. Do not treat
`fetch()` below as live.

There is no Railinc-equivalent car-level tracking here, and none is
planned for this adapter -- see river_surveillance.py's docstring on why
rail has no AIS-equivalent open broadcast signal. This is macro volume/
dwell context only, never company-specific, same tier as the FRED
credit-conditions signal.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

# Placeholder -- not a confirmed live endpoint. Replace once a real AAR/
# STB data-access method (API, scheduled file pull, etc.) is confirmed.
BASE_URL = "https://railroadpm.org/api"  # UNCONFIRMED


class RailTrafficAdapter(TelemetryAdapter):
    source = TelemetrySource.RAIL_TRAFFIC

    def fetch(self) -> list[RawTelemetryRecord]:
        """UNCONFIRMED against live docs -- see module docstring. Written
        so the classification/wiring downstream (silo_leadgen.py::
        _rail_congestion_signals) has something real to call once a
        confirmed access method replaces this."""
        records: list[RawTelemetryRecord] = []
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.get("/service-metrics", params={"api_key": self.api_key})
            response.raise_for_status()
            for item in response.json().get("results", []):
                records.append(
                    RawTelemetryRecord(
                        title=f"Rail service metric: {item.get('railroad', 'unknown')} {item.get('terminal', '')}",
                        payload=item,
                    )
                )
        return records
