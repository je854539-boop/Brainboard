"""SeaVantage ocean cargo / container tracking adapter.

CORRECTION (checked against seavantage.com directly): this is NOT just a
vessel-position/ETA tracker in the same tier as Datalastic/VesselFinder --
SeaVantage's real product (the "Cargo Report" module inside their Cargo
Insight line) does genuine CONTAINER-LEVEL tracking: per-container dwell
time from arrival to gate-out, compared against each shipment's actual
free-time allowance, with a "last free day" field/alert, full milestone
history (empty-equipment dispatch through port-of-discharge arrival), and
real terminal integrations (APM Terminals and others). That's exactly the
demurrage/detention signal services/silo_leadgen.py's
_demurrage_risk_note needs, and more precise than that function's
own fallback (today - arrival_date > a reasoned 5-day default) --
SeaVantage's own last-free-day field reflects the ACTUAL contractual
free time for that specific shipment, not a guessed default. Their
Predicted Time of Arrival feature already folds in AIS + port-congestion
data internally, which is also why GFW isn't a needed complement here --
SeaVantage already does its own AIS-based read.

CONFIRMED (re-checked directly against seavantage.com, more precise
than the earlier guess): SeaVantage's real developer API has TWO
tiers, not one undifferentiated surface. The Core API covers shipment
tracking/milestones via a real, named endpoint -- GET /cargo/search --
which is what this adapter now targets, replacing the earlier "/v1/
cargo-report" guess. A separate Extended API tier adds live AIS vessel
positions, vessel profiles, historical tracks, and navigation data --
not called here, since the Core API's cargo/milestone surface is the
one this project actually needs (see _demurrage_risk_note); the
Extended tier would be the thing to reach for if per-container AIS
correlation ever becomes its own use case.

REMAINING UNCONFIRMED: the exact response field names (still passed
through unmodified below, same defensive posture as every other
adapter), and SeaVantage's own "REST API - 2h refresh" label, which
directly conflicts with their "real-time" marketing copy -- confirm
the actual refresh interval against your real account/API plan before
treating this as live data, especially for anything time-sensitive
like the last-free-day demurrage check.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.seavantage.com"  # placeholder -- confirm against your SeaVantage API plan


class SeaVantageAdapter(TelemetryAdapter):
    source = TelemetrySource.SEAVANTAGE

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30) as client:
            # /cargo/search is SeaVantage's real, documented Core API
            # endpoint (confirmed against their developer docs) --
            # params/query shape still TODO against your actual plan.
            # Each returned item is expected to carry a container/
            # shipment identifier, consignee, and dwell/last-free-day
            # fields -- passed through unmodified so
            # _demurrage_risk_note's defensive extraction can read
            # whichever field name the real response actually uses.
            response = client.get("/cargo/search")
            response.raise_for_status()
            payload = response.json()

        return [
            RawTelemetryRecord(title=item.get("consignee_name") or item.get("container_number", "Cargo telemetry"), payload=item)
            for item in payload.get("containers", payload.get("results", []))
        ]
