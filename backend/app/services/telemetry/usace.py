"""USACE inland-waterway lock status/closure macro telemetry sweep.

Feeds the CME Macro Funnel silo: a lock closure or extended delay on the
Mississippi/Illinois/Ohio river system disrupts grain-barge traffic --
which is a genuine leading indicator for Chicago commodities-market
(CME) volatility on the agricultural futures side (corn, wheat,
soybeans), well ahead of any price move showing up on CME Globex itself.

I don't have a confirmed, live, keyless USACE REST endpoint for lock
status to hardcode with confidence -- USACE's Lock Performance Monitoring
System (LPMS) and Navigation Data Center publish this data, but not (as
far as I can verify from this environment) behind a single stable modern
API. This ships as a placeholder BASE_URL against a plausible lock-status
endpoint, gated by USACE_API_KEY like every other adapter in this file so
it's inert rather than silently wrong until you've confirmed the actual
endpoint against whatever USACE/NDC data source your ops team has access
to (bulk CSV export, Socrata portal, or a paid aggregator) and pointed
this adapter at it.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.usace.army.mil"  # placeholder -- confirm against your actual USACE/NDC data source

# Locks on the grain-barge corridors most relevant to Chicago commodities
# flow (Illinois Waterway, Upper/Middle Mississippi, Ohio) -- narrow or
# widen this list to the corridors relevant to your tracked leads.
MONITORED_LOCKS = [
    "Lock and Dam 1 (Mississippi)",
    "Lock and Dam 19 (Mississippi)",
    "Melvin Price Locks and Dam (Mississippi)",
    "Lockport Lock (Illinois Waterway)",
    "Marseilles Lock (Illinois Waterway)",
    "Peoria Lock (Illinois Waterway)",
]


class USACEAdapter(TelemetryAdapter):
    source = TelemetrySource.USACE

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            # TODO: confirm the actual lock-status endpoint path/params
            # against your USACE/NDC data source.
            response = client.get("/lpms/lock-status", params={"api_key": self.api_key, "locks": ",".join(MONITORED_LOCKS)})
            response.raise_for_status()
            payload = response.json()

        records = []
        for lock in payload.get("locks", []):
            status = lock.get("status", "unknown")
            if status.lower() == "open":
                continue  # only closures/delays are lead-gen signal, not routine open status
            records.append(RawTelemetryRecord(title=f"USACE lock disruption: {lock.get('name', 'unknown lock')} ({status})", payload=lock))
        return records
