"""Shared company-name entity resolution -- Interzoid fuzzy matching with
a substring fallback when Interzoid isn't configured.

Interzoid (interzoid.com) is the same data-quality provider already used
for per-lead enrichment (enrichment/interzoid.py); this adds its
name-matching product as a general-purpose, non-lead-specific utility so
telemetry-sweep code (silo_leadgen.py's Cobalt/Apollo stage matching,
river_surveillance.py's entity resolution) can confirm two company-name
strings are probably the same real business, instead of relying on raw
substring overlap -- which misses real matches on legal-name variants
(DBAs, LLC/Inc suffixes, punctuation) far more often than it produces
false ones. This was the single most repeated caveat across every
funnel/waterfall stage before Interzoid was wired in for this purpose.

Endpoint path and response field names below are unconfirmed against
live Interzoid docs (same caveat as every other Interzoid/Cobalt/Apollo
integration in this project) -- name_match_score() defensively checks
several plausible response key names and returns None on any failure
(bad key, wrong endpoint, network error) rather than raising, so callers
degrade to substring matching instead of losing the comparison outright.
Until this is confirmed against a real account, treat it the same as
every "unconfirmed -- verify against your live plan" adapter elsewhere.
"""

import logging

import httpx

logger = logging.getLogger("brainboard.entity_matching")

BASE_URL = "https://api.interzoid.com"  # same base as enrichment/interzoid.py
DEFAULT_MATCH_THRESHOLD = 80.0  # Interzoid similarity score (0-100) required to call two names the same entity


def name_match_score(api_key: str, name_a: str, name_b: str) -> float | None:
    """Fuzzy company-name similarity via Interzoid's name-matching API,
    same `license` query-param auth as enrichment/interzoid.py's Get
    Company Data call. Returns None (never raises) on any failure, so a
    caller can fall back to substring matching instead of losing the
    comparison outright."""
    try:
        with httpx.Client(base_url=BASE_URL, timeout=15) as client:
            response = client.get(
                "/getcompanynamematch/v1/companynamematch",
                params={"license": api_key, "name1": name_a, "name2": name_b},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 -- a bad match call must not break entity resolution
        logger.warning("Interzoid name-match call failed for %r vs %r: %s", name_a, name_b, exc)
        return None

    for key in ("Similarity", "similarity", "Score", "score", "MatchScore"):
        value = payload.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def names_match(api_key: str, name_a: str | None, name_b: str | None, threshold: float = DEFAULT_MATCH_THRESHOLD) -> bool:
    """True if name_a and name_b are (probably) the same company. Uses
    Interzoid fuzzy scoring when api_key is set and the call succeeds;
    otherwise falls back to substring containment -- the exact heuristic
    every funnel stage used before Interzoid was wired in here, so
    behavior only ever improves, never regresses, once a key is added."""
    if not name_a or not name_b:
        return False
    a, b = name_a.strip().lower(), name_b.strip().lower()
    if not a or not b:
        return False

    if api_key:
        score = name_match_score(api_key, name_a, name_b)
        if score is not None:
            return score >= threshold

    return a in b or b in a
