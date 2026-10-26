"""ADS-B (Automatic Dependent Surveillance-Broadcast) adapter -- the real
AIS-equivalent for aircraft.

Unlike railcars or trucks, most cargo aircraft broadcast position
openly via ADS-B, picked up by open/commercial networks (ADS-B Exchange,
FlightAware, Flightradar24). That open-broadcast signal is what makes
air cargo a genuinely closeable blind spot, unlike rail/trucking where
no equivalent open signal exists at all.

HONESTY NOTE, same posture as rail_traffic.py: this is written against
a plausible provider shape (ADS-B Exchange's REST API), but the exact
live endpoint/auth contract has not been independently re-verified
against current docs in this sandbox -- confirm before relying on it.

Scope, deliberately trimmed: this is a macro/classification-level
signal only (silo_leadgen.py::_adsb_air_cargo_signals), not a full
airport-zone surveillance engine mirroring river_surveillance.py's idle-
vessel detection. Building that (named cargo-carrier callsigns, airport
zones, idle-on-ramp detection) is real future scope, not built here.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.adsbexchange.com/v2"  # UNCONFIRMED -- verify against current ADS-B Exchange docs

# Real cargo-carrier ICAO callsign prefixes -- FedEx Express, UPS
# Airlines, Atlas Air (operates for Amazon Air among others), Kalitta
# Air. Not exhaustive; good enough to flag "this is a cargo operator,"
# not every possible one.
_CARGO_CALLSIGN_PREFIXES = ("FDX", "UPS", "GTI", "CKS")


class ADSBAdapter(TelemetryAdapter):
    source = TelemetrySource.ADSB

    def fetch(self) -> list[RawTelemetryRecord]:
        """UNCONFIRMED against live docs -- see module docstring."""
        records: list[RawTelemetryRecord] = []
        with httpx.Client(base_url=BASE_URL, timeout=30, headers={"api-auth": self.api_key}) as client:
            response = client.get("/aircraft/all")
            response.raise_for_status()
            for aircraft in response.json().get("ac", []):
                callsign = (aircraft.get("flight") or "").strip()
                if not any(callsign.startswith(prefix) for prefix in _CARGO_CALLSIGN_PREFIXES):
                    continue
                records.append(
                    RawTelemetryRecord(
                        title=f"ADS-B cargo aircraft: {callsign} near {aircraft.get('lat')},{aircraft.get('lon')}",
                        payload=aircraft,
                    )
                )
        return records
