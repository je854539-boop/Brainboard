"""TransmetriQ adapter -- the real car-level rail answer, replacing the
"Railinc requires a shipper-of-record relationship" dead end flagged
earlier this session.

CONFIRMED real (via web search, not assumed): TransmetriQ is Railinc's
own modernized SaaS/API layer over the same underlying rail-industry
infrastructure (Umler car registry, interchange events) that raw
Railinc access requires direct EDI/XML membership for. It offers
VIN-level tracking across 600+ North American railroads (up to 1,000
VINs per request per a TransmetriQ press release) and clean JSON
delivery instead of legacy EDI. Pricing is quote-based with no public
dollar figure; a 60-day free trial exists for invited Railinc users.

HONESTY NOTE, same posture as every other new-provider adapter this
session: confirming the PRODUCT is real is not the same as confirming
its exact live endpoint contract (auth scheme, request/response shape)
against current docs, which this sandbox cannot reach to verify. This
is written against TransmetriQ's publicly described capability (car-
location/interchange event lookup), not a guessed vendor.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.transmetriq.com/v1"  # UNCONFIRMED -- verify against current TransmetriQ docs


class TransmetriQAdapter(TelemetryAdapter):
    source = TelemetrySource.TRANSMETRIQ

    def fetch(self) -> list[RawTelemetryRecord]:
        """UNCONFIRMED against live docs -- see module docstring. Real
        car-level identity (a company name on the shipment) is the
        actual advantage over RAIL_TRAFFIC's AAR/STB macro placeholder,
        once this is wired to a real endpoint -- see
        silo_leadgen.py::_transmetriq_rail_signals."""
        records: list[RawTelemetryRecord] = []
        with httpx.Client(base_url=BASE_URL, timeout=30, headers={"Authorization": f"Bearer {self.api_key}"}) as client:
            response = client.get("/events/recent")
            response.raise_for_status()
            for item in response.json().get("events", []):
                records.append(
                    RawTelemetryRecord(
                        title=f"TransmetriQ rail event: {item.get('event_type', 'unknown')} -- {item.get('consignee_name', 'unknown shipper')}",
                        payload=item,
                    )
                )
        return records
