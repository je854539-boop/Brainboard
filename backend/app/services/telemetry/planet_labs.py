"""Planet Labs adapter -- daily-revisit optical land-use/parcel change
detection, confirmed real pricing via web search: Monitoring-plan tiers
run $2,700-$9,650/year depending on coverage area (planet.com/pricing).

IMPORTANT distinction, worth repeating since it's easy to blur: Planet
Labs is optical (PlanetScope/SkySat), NOT radar. It's the right tool for
parcel-scale change detection against a real historical baseline (new
construction, cleared land, expanded operations -- pairs with Regrid
for ownership), paired with daily revisit that beats free Sentinel-2's
~5-day cadence. It is NOT the cloud-penetrating disaster-damage tool --
that's Sentinel-1 SAR (free, via Sentinel Hub) or a dedicated SAR
provider (ICEYE, Capella Space), which can see through the cloud cover
a real storm brings and Planet's optical sensors cannot.

HONESTY NOTE: written against Planet's publicly documented API family
(Subscriptions API for continuous feed over an area of interest), but
the exact live endpoint contract/response shape is unconfirmed against
current docs in this sandbox.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter

BASE_URL = "https://api.planet.com"  # UNCONFIRMED exact path -- verify against current Planet API docs


class PlanetLabsAdapter(TelemetryAdapter):
    source = TelemetrySource.PLANET_LABS

    def fetch(self) -> list[RawTelemetryRecord]:
        """UNCONFIRMED against live docs -- see module docstring. Real
        deployment would be subscription-driven (Planet pushes change
        results for a pre-registered area of interest) rather than a
        plain poll -- this fetch() stands in for that until a real
        subscription webhook/pull is wired in."""
        records: list[RawTelemetryRecord] = []
        with httpx.Client(base_url=BASE_URL, timeout=30, headers={"Authorization": f"api-key {self.api_key}"}) as client:
            response = client.get("/subscriptions/v1/results")
            response.raise_for_status()
            for item in response.json().get("results", []):
                records.append(
                    RawTelemetryRecord(
                        title=f"Planet Labs land-use change: parcel {item.get('parcel_id', 'unknown')} -- change score {item.get('change_score')}",
                        payload=item,
                    )
                )
        return records
