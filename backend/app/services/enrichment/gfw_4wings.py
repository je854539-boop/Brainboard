"""GFW 4Wings per-lead enrichment.

GFW's 4Wings API reports gridded marine traffic for a bounding box, not a
company lookup -- there's no "search by business name" here. This
attaches regional marine-traffic context when the lead has a state on
file (useful for maritime-adjacent leads -- logistics, oil & gas -- that
a phone call wouldn't have covered). Raises (skipped, not an error) when
there's no state to scope the query to.
"""

import datetime as dt

import httpx

from app.config import get_settings
from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://gateway.api.globalfishingwatch.org/v3"


class GFW4WingsEnrichmentAdapter(EnrichmentAdapter):
    source = TelemetrySource.GFW_4WINGS

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.state:
            raise ValueError("GFW 4Wings enrichment requires a state to scope the regional query")

        end = dt.date.today()
        start = end - dt.timedelta(days=7)
        with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30) as client:
            # TODO: map query.state to an actual bounding box for its
            # coastline/ports rather than pulling the global sweep.
            response = client.post(
                "/4wings/report",
                params={"date-range": f"{start.isoformat()},{end.isoformat()}", "spatial-resolution": "low"},
                json={"dataset": get_settings().gfw_dataset},
            )
            response.raise_for_status()
            return {"region": query.state, "note": "regional macro context, not specific to this business", "marine_traffic_snapshot": response.json()}
