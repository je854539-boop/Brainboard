"""Federal Reserve Economic Data (FRED) adapter -- a macro credit-
conditions context signal, not a commodity or location-specific one.

Tighter conventional bank lending in general means more small businesses
are likely to need alternative capital (MCA) regardless of any commodity
or disaster signal already active -- this is context layered on top of
everything else, same posture as how SONAR's trucking-capacity data was
scoped: a pressure reading, not a company-pointer on its own.

HONESTY NOTE: FRED's API itself (api.stlouisfed.org) is real, free, and
well-documented, and requires only a free signup key. The specific
series ID below (the Senior Loan Officer Opinion Survey's net
percentage of banks tightening C&I lending standards for small firms)
is the right real-world series conceptually, but the exact FRED series
ID string has not been independently re-verified against live docs in
this sandbox -- confirm it against https://fred.stlouisfed.org before
relying on it, same posture as every other "unconfirmed against live
docs" adapter in this project (see telemetry/import_genius.py,
telemetry/seavantage.py, river_surveillance.py's LPMS note).
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.stlouisfed.org/fred"

# Senior Loan Officer Opinion Survey: net % of banks reporting tighter
# C&I lending standards for small firms. Verify this exact series ID
# against FRED's own site before going live -- see module docstring.
CREDIT_TIGHTENING_SERIES_ID = "DRTSCLCC"


class FREDAdapter(TelemetryAdapter):
    source = TelemetrySource.FRED

    def fetch(self) -> list[RawTelemetryRecord]:
        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.get(
                "/series/observations",
                params={
                    "series_id": CREDIT_TIGHTENING_SERIES_ID,
                    "api_key": self.api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 1,
                },
            )
            response.raise_for_status()
            observations = response.json().get("observations", [])

        if not observations:
            return []

        latest = observations[0]
        return [
            RawTelemetryRecord(
                title=f"FRED credit-tightening reading ({latest.get('date')}): {latest.get('value')}",
                payload=latest,
            )
        ]
