"""Secretary of State corporate registry adapter.

Same shape as UCC filings: each state publishes its own business-entity
registry (some via Socrata/CKAN open-data APIs, others only via web
lookup with no public API). Configure one HTTP source per state in
`STATE_ENDPOINTS` and implement that state's parsing in `fetch()`.
Adapter is inert until at least one endpoint is configured.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

STATE_ENDPOINTS: dict[str, dict] = {}


class SOSRegistriesAdapter(TelemetryAdapter):
    source = TelemetrySource.SOS_REGISTRIES

    def __init__(self):
        super().__init__(api_key="configured" if STATE_ENDPOINTS else "")

    def fetch(self) -> list[RawTelemetryRecord]:
        records: list[RawTelemetryRecord] = []
        for state, config in STATE_ENDPOINTS.items():
            with httpx.Client(headers=config.get("headers", {}), timeout=30) as client:
                response = client.get(config["url"])
                response.raise_for_status()
                # TODO: normalize per that state's actual response schema.
                for item in response.json().get("entities", []):
                    records.append(
                        RawTelemetryRecord(
                            title=f"SOS registry ({state}): {item.get('entity_name', 'unknown entity')}",
                            payload=item,
                        )
                    )
        return records
