"""FMCSA Company Snapshot / QCMobile adapter -- real fleet-size
verification (power units, driver count, safety rating) for any
logistics-flavored lead, confirmed free this session via the user's own
screenshots of FMCSA's live API Access docs and Carrier Search screen.

CONFIRMED real: a free login.gov-backed developer account issues a
WebKey (FMCSA Developer Site -> "My WebKeys" -> "Get a new WebKey"),
appended as a `webKey` query param on every call -- no fee mentioned
anywhere in FMCSA's own docs. The live Carrier Search screen shows three
distinct search fields -- Carrier Name, DOT #, MC # -- confirming
name-based search is a real capability, not just DOT/MC lookup as first
assumed.

HONESTY NOTE, same posture as transmetriq.py/rail_traffic.py/adsb.py:
confirming the auth pattern and that name-search exists is not the same
as confirming the exact live JSON response shape for a name-search call
against the authenticated QCMobile REST API (only the auth/basics page
was seen, not per-endpoint response docs). `_extract_power_units` below
defensively checks several plausible field-name variants and returns
None (not a guessed 0) on anything it doesn't recognize, same degrade-
gracefully posture as entity_matching.py's name_match_score. Confirm the
exact response schema against a live WebKey before trusting
`total_power_units` in production. Bulk/filter-by-fleet-size querying is
NOT confirmed to exist at all -- this adapter is a one-name-at-a-time
lookup, same single-entity shape as every other EnrichmentAdapter here.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://mobile.fmcsa.dot.gov/qc/services"  # UNCONFIRMED exact path -- verify against a live WebKey


def _first_match(payload: dict) -> dict | None:
    """QCMobile's documented shape wraps results in a "content" list on
    search endpoints; a single-DOT lookup instead returns the carrier
    object directly. Handles both without assuming which one a given
    call returned."""
    content = payload.get("content")
    if isinstance(content, list) and content:
        return content[0]
    if isinstance(payload.get("carrier"), dict):
        return payload["carrier"]
    return payload if payload.get("dotNumber") or payload.get("legalName") else None


def _extract_power_units(carrier: dict) -> float | None:
    for key in ("totalPowerUnits", "totalpowerunits", "powerUnits", "total_power_units"):
        value = carrier.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _extract_driver_count(carrier: dict) -> float | None:
    for key in ("totalDrivers", "totaldrivers", "driverTotal", "total_drivers"):
        value = carrier.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


class FMCSAAdapter(EnrichmentAdapter):
    source = TelemetrySource.FMCSA

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.business_name:
            raise ValueError("FMCSA carrier lookup requires business_name")

        with httpx.Client(base_url=BASE_URL, timeout=30) as client:
            response = client.get(
                f"/carriers/name/{query.business_name}",
                params={"webKey": self.api_key},
            )
            response.raise_for_status()
            payload = response.json()

        carrier = _first_match(payload)
        if carrier is None:
            return {"matched": False, "raw": payload}

        return {
            "matched": True,
            "dot_number": carrier.get("dotNumber"),
            "legal_name": carrier.get("legalName"),
            "dba_name": carrier.get("dbaName"),
            "total_power_units": _extract_power_units(carrier),
            "total_drivers": _extract_driver_count(carrier),
            "safety_rating": carrier.get("safetyRating"),
            "raw": carrier,
        }
