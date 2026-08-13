"""openFDA macro sweep adapter -- Healthcare & Pharma Silo lead-gen.

Surfaces recent FDA enforcement (recall) actions across drug/device/food
categories as a lead-gen signal: a company hit with a recall often needs
financing to cover remediation, inventory write-off, or legal costs. This
is the passive-sweep counterpart to enrichment/openfda.py's per-lead
compliance check -- same free/keyless API, different query mode.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.fda.gov"


class OpenFDATelemetryAdapter(TelemetryAdapter):
    source = TelemetrySource.OPENFDA

    @property
    def enabled(self) -> bool:
        return True  # public API; api_key (if set) only raises the rate limit

    def fetch(self) -> list[RawTelemetryRecord]:
        records: list[RawTelemetryRecord] = []
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            for endpoint in ("drug/enforcement.json", "device/enforcement.json", "food/enforcement.json"):
                params = {"limit": 20, "sort": "report_date:desc"}
                if self.api_key:
                    params["api_key"] = self.api_key
                response = client.get(f"/{endpoint}", params=params)
                if response.status_code == 404:
                    continue  # openFDA 404s when a query matches zero records
                response.raise_for_status()
                for item in response.json().get("results", []):
                    records.append(
                        RawTelemetryRecord(
                            title=f"FDA enforcement: {item.get('recalling_firm', 'unknown firm')}",
                            payload=item,
                        )
                    )
        return records
