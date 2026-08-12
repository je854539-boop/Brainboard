"""UCC-1/UCC-3 lien filing adapter.

There is no single national UCC API -- filings are published per
Secretary of State, and formats vary (Socrata open-data portals, vendor
aggregators like UCC Retrievals/Corporation Service Company, or per-state
bulk-data files). Configure one HTTP source per state you operate in via
`STATE_ENDPOINTS` below, keyed by two-letter state code, then implement
that state's response parsing in `fetch()`. Adapter is inert until at
least one endpoint is configured.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

# state -> { "url": ..., "headers": {...} }. Populate per the UCC data
# source(s) your ops team has access to for each state of operation.
STATE_ENDPOINTS: dict[str, dict] = {}


class UCCFilingsAdapter(TelemetryAdapter):
    source = TelemetrySource.UCC_FILINGS

    def __init__(self):
        super().__init__(api_key="configured" if STATE_ENDPOINTS else "")

    def fetch(self) -> list[RawTelemetryRecord]:
        records: list[RawTelemetryRecord] = []
        for state, config in STATE_ENDPOINTS.items():
            with httpx.Client(headers=config.get("headers", {}), timeout=30) as client:
                response = client.get(config["url"])
                response.raise_for_status()
                # TODO: normalize per that state's actual response schema.
                for item in response.json().get("filings", []):
                    records.append(
                        RawTelemetryRecord(
                            title=f"UCC filing ({state}): {item.get('debtor_name', 'unknown debtor')}",
                            payload=item,
                        )
                    )
        return records
