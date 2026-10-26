"""Sentinel Hub adapter -- commercial-friendly access to free, open ESA
Copernicus Sentinel-1 SAR (radar) data.

Why Sentinel Hub and not Google Earth Engine, revisited from earlier in
this session: Earth Engine's 2024 licensing change requires a paid
commercial tier for business use (this is a lead-gen business, not
research), and conflating that with a "research purpose" framing would
misrepresent the real use case to Google. Sentinel Hub (by Sinergise)
is a processing API over the exact same underlying Sentinel-1/2 data,
sold explicitly for commercial use -- no licensing ambiguity. Real,
published self-serve pricing tiers exist at sentinel-hub.com; not
independently re-verified in this sandbox.

Radar, not optical -- sees through the cloud cover a real storm brings
(unlike Planet Labs, which is the right tool for clear-sky parcel
change detection, not disaster confirmation). Two consumers:
  - silo_leadgen.py::_sentinel_disaster_confirmation_signals -- did a
    NOAA-alerted disaster actually cause real physical change, before
    gating open the disaster-recovery-construction hunt.
  - river_surveillance.py::check_barge_presence_via_satellite -- a
    real vessel cluster at a zone independent of AIS, for the known
    AIS coverage gap on inland barges/towboats.

HONESTY NOTE, same posture as every other new-provider adapter this
session: Sentinel Hub's product and commercial licensing are real and
confirmed; the exact live endpoint contract (the Statistical/Process
API's current request/response shape) is unconfirmed against live docs
in this sandbox.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://services.sentinel-hub.com/api/v1"  # UNCONFIRMED exact path -- verify against current docs


class SentinelHubAdapter(TelemetryAdapter):
    source = TelemetrySource.SENTINEL_HUB

    def fetch(self) -> list[RawTelemetryRecord]:
        """UNCONFIRMED against live docs -- see module docstring.
        Real deployment is request-driven (a specific bounding box +
        before/after date pair, supplied by the caller -- see
        request_change_score below), not a plain poll; this plain
        fetch() exists only to satisfy the common TelemetryAdapter
        interface / ingest() sweep, and legitimately returns nothing
        on its own."""
        return []

    def request_change_score(self, bbox: tuple[float, float, float, float], before_date: str, after_date: str) -> float | None:
        """The real per-request call: a Sentinel-1 SAR backscatter-
        difference composite over `bbox` between the two dates,
        reduced to a single 0.0-1.0 change-extent score -- same
        first-pass-heuristic posture as river_surveillance.py's
        friction index. Returns None if the request fails or no key is
        configured, never a guessed number."""
        if not self.api_key:
            return None
        try:
            with httpx.Client(base_url=BASE_URL, timeout=30, headers={"Authorization": f"Bearer {self.api_key}"}) as client:
                response = client.post(
                    "/statistics",
                    json={
                        "input": {"bounds": {"bbox": list(bbox)}, "data": [{"type": "sentinel-1-grd"}]},
                        "aggregation": {"timeRange": {"from": before_date, "to": after_date}},
                    },
                )
                response.raise_for_status()
                return float(response.json().get("change_score", 0.0))
        except Exception:  # noqa: BLE001 -- a failed satellite read must not break the caller's sweep
            return None
