"""FreightWaves SONAR adapter -- trucking macro capacity/tightness
context, confirmed real via web search (self-serve tier ~$25-35/mo,
full API access enterprise/quote-based).

Same honest limit discussed extensively this session: there is no
AIS-equivalent open broadcast for trucks, so SONAR's tender-rejection/
outbound-tender-volume indices are a macro pressure reading, never a
company-specific pointer. Useful as context layered onto a company
already identified through another channel (ocean, rail, GDELT), not
a discovery mechanism on its own -- same role FRED plays for credit
conditions.

HONESTY NOTE: written against SONAR's publicly described index
structure (lane/market-level tender-rejection rate), not a guessed
vendor, but the exact live endpoint contract is unconfirmed against
current docs in this sandbox.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.sonar.freightwaves.com"  # UNCONFIRMED -- verify against current SONAR docs


class SONARAdapter(TelemetryAdapter):
    source = TelemetrySource.SONAR

    def fetch(self) -> list[RawTelemetryRecord]:
        """UNCONFIRMED against live docs -- see module docstring."""
        records: list[RawTelemetryRecord] = []
        with httpx.Client(base_url=BASE_URL, timeout=30, headers={"Authorization": f"Bearer {self.api_key}"}) as client:
            response = client.get("/v1/outbound-tender-reject-index", params={"granularity": "national"})
            response.raise_for_status()
            for item in response.json().get("data", []):
                records.append(
                    RawTelemetryRecord(
                        title=f"SONAR outbound tender reject index: {item.get('value')}% ({item.get('date')})",
                        payload=item,
                    )
                )
        return records
