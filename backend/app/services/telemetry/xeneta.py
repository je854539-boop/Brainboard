"""Xeneta adapter -- air-freight rate/capacity intelligence, the real
missing complement to ADS-B.

CONFIRMED real via web search: Xeneta benchmarks air freight rates
across 30,000+ airport pairs, daily shipper rates and weekly airline
rates, subscription/enterprise pricing (quote-based, no public dollar
figure). WorldACD is a comparable real alternative (100-115 airlines,
its own rate index and capacity/load-factor reports) -- not built here,
same one-primary-pick discipline as choosing Sentinel Hub over Earth
Engine earlier this build.

Where this differs from SONAR/FRED: those are deliberately nationwide,
commodity-less context signals with no location to anchor a hunt to.
Xeneta's data is lane-specific (an origin/destination airport pair),
so a real rate spike on a specific lane CAN carry a real destination
city -- see silo_leadgen.py::_xeneta_air_freight_signals, which uses
that location the same way USACE's place-anchored signals drive a
real Apollo/Cobalt hunt, not just nationwide pressure context.

HONESTY NOTE, same posture as every other new-provider adapter this
session: confirming the product and its data scope is real is not the
same as confirming the exact live endpoint contract, which this
sandbox cannot reach to verify.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.xeneta.com/v3"  # UNCONFIRMED -- verify against current Xeneta docs


class XenetaAdapter(TelemetryAdapter):
    source = TelemetrySource.XENETA

    def fetch(self) -> list[RawTelemetryRecord]:
        """UNCONFIRMED against live docs -- see module docstring."""
        records: list[RawTelemetryRecord] = []
        with httpx.Client(base_url=BASE_URL, timeout=30, headers={"Authorization": f"Bearer {self.api_key}"}) as client:
            response = client.get("/benchmarks/air", params={"mode": "air"})
            response.raise_for_status()
            for item in response.json().get("lanes", []):
                origin = item.get("origin_airport", "unknown origin")
                destination = item.get("destination_airport", item.get("destination_city", "unknown destination"))
                records.append(
                    RawTelemetryRecord(
                        title=f"Xeneta air rate: {origin} -> {destination}, rate index {item.get('rate_index')}",
                        payload=item,
                    )
                )
        return records
