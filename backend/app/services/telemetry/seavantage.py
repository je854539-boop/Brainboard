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

Endpoint/field names below are a reasoned best guess at their "cargo
report" surface, NOT confirmed against SeaVantage's live API docs --
same honesty posture as every other adapter in this codebase. A
third-party review claims their API refreshes on a ~2-hour cycle, not
real-time -- unverified, flagged here rather than assumed. Confirm both
against your actual SeaVantage account/API plan before relying on this.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.seavantage.com"  # placeholder -- confirm against your SeaVantage API plan


class SeaVantageAdapter(TelemetryAdapter):
    source = TelemetrySource.SEAVANTAGE

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30) as client:
            # TODO: confirm the real cargo-report/container-tracking
            # endpoint and params against SeaVantage's live API docs --
            # "/v1/cargo-report" is a reasoned guess, not a confirmed
            # path. Each returned item is expected to carry a container/
            # shipment identifier, consignee, and dwell/last-free-day
            # fields -- passed through unmodified so
            # _demurrage_risk_note's defensive extraction can read
            # whichever field name the real response actually uses.
            response = client.get("/v1/cargo-report")
            response.raise_for_status()
            payload = response.json()

        return [
            RawTelemetryRecord(title=item.get("consignee_name") or item.get("container_number", "Cargo telemetry"), payload=item)
            for item in payload.get("containers", payload.get("results", []))
        ]
