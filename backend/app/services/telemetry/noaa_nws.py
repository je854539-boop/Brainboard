"""NOAA National Weather Service alerts adapter -- disaster-recovery
construction lead-gen, and a better primary source than GDELT for "did a
real weather disaster just happen."

api.weather.gov is a real, free, keyless, well-documented US government
API. Its /alerts/active endpoint returns a GeoJSON FeatureCollection of
currently active watches/warnings/advisories, each carrying a structured
`properties.event` (e.g. "Hurricane Warning", "Flood Warning"),
`properties.areaDesc` (plain-text affected area), and `properties.severity`.
This is issued the moment NOAA declares the alert -- it doesn't depend on
a journalist writing about it first, which is the real advantage over
relying on GDELT coverage alone for disaster detection (see
silo_leadgen.py::_noaa_weather_disaster_signals, which classifies these
into the disaster_recovery_construction commodity).
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.weather.gov"

# Only alert types with a real, direct disaster-recovery-construction
# demand story -- a "Dense Fog Advisory" or "Heat Advisory" doesn't
# generate cleanup/rebuilding work the way these do.
_DISASTER_EVENT_KEYWORDS = (
    "hurricane",
    "tornado",
    "flood",
    "flash flood",
    "tropical storm",
    "storm surge",
    "winter storm",
    "ice storm",
    "high wind",
)


class NOAANWSAdapter(TelemetryAdapter):
    source = TelemetrySource.NOAA_NWS

    @property
    def enabled(self) -> bool:
        return True  # public, keyless API -- no key gates this on/off

    def fetch(self) -> list[RawTelemetryRecord]:
        records: list[RawTelemetryRecord] = []
        with httpx.Client(
            base_url=BASE_URL,
            timeout=30,
            headers={"User-Agent": "Brainboard (lead-gen disaster-recovery signal)"},
        ) as client:
            response = client.get(
                "/alerts/active",
                params={"status": "actual", "message_type": "alert"},
            )
            response.raise_for_status()
            features = response.json().get("features", [])
            for feature in features:
                props = feature.get("properties", {})
                event = (props.get("event") or "").lower()
                if not any(keyword in event for keyword in _DISASTER_EVENT_KEYWORDS):
                    continue
                records.append(
                    RawTelemetryRecord(
                        title=f"NOAA NWS {props.get('event', 'alert')}: {props.get('areaDesc', 'unknown area')}",
                        payload=props,
                    )
                )
        return records
