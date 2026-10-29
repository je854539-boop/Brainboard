"""Free, keyword-based industry classifier for purchased-batch rows that
carry no (or an unmapped) industry field of their own -- used by both
ucc_batch.py and mca_history_batch.py as a fallback onto the same
dialer.INDUSTRY_INBOUND_GREETINGS vocabulary that a real Industry column
maps onto (see mca_history_batch.py::INDUSTRY_TAG_MAP).

Dry-run tested against the real 18,402-row MCA History Batch file's
13,091 rows with no Industry value: word-boundary matching (not naive
substring -- an earlier substring-only pass mistagged "...marketing
group" as Food Processing via "market" inside "marketing") tagged 2,809
of them (21.5%), zero cost, no API call. Precision over recall by
design -- a business name that doesn't clearly self-describe gets no
tag rather than a guessed one.

Deliberately covers only the 5 of 9 INDUSTRY_INBOUND_GREETINGS buckets a
business NAME can plausibly self-describe (Trucking & Logistics, Heavy
Machinery & Industrial Equipment, Food Processing & Fabrication,
Healthcare & Pharma, E-Commerce & Fulfillment) -- Tariff, Oil & Gas /
Refining, Agriculture & Grain Handling, and HigherGov aren't things a
generic small-business name advertises, so guessing those from a name
alone would be noise, not signal.

NOT wired into per-call greeting selection for an already-known caller
(personalized_greeting_for_caller uses SIGNAL_PHRASES keyed by
TelemetrySource, unaffected by this) -- this tag's real jobs are: (1)
sheet/notes transparency on why a candidate was bucketed, (2) ad
targeting / landing-page segmentation off SiloCandidate.industry_tag,
and (3) which industry-tagged DialerNumberPool number a campaign for
this segment should dial out from, so a callback that doesn't ANI-match
still lands on a number whose industry_greeting_for_number fallback
fits.
"""

import re

# Ordered (first match wins) word-boundary regex patterns per target
# industry-pool tag. "market" is deliberately given a negative
# lookahead for "ing" so "marketing" never false-positives into Food
# Processing & Fabrication the way a naive substring check did.
KEYWORD_INDUSTRY_PATTERNS: dict[str, list[str]] = {
    "Trucking & Logistics": [
        r"\btruck\w*", r"\blogistic\w*", r"\bfreight\b", r"\bhauling\b",
        r"\btransport\w*", r"\bhotshot\w*", r"\bdelivery\b", r"\bcarrier\b",
    ],
    "Heavy Machinery & Industrial Equipment": [
        r"\bconstruction\b", r"\bcontractor\w*", r"\bbuilder\w*", r"\bexcavat\w*",
        r"\bconcrete\b", r"\broofing\b", r"\belectric\w*", r"\bplumb\w*",
        r"\bpaving\b", r"\blandscap\w*", r"\bhvac\b", r"\bfence\b",
        r"\bmasonry\b", r"\bwelding\b",
    ],
    "Food Processing & Fabrication": [
        r"\brestaurant\b", r"\bcafe\b", r"\bdiner\b", r"\bgrill\b", r"\bkitchen\b",
        r"\bfood\b", r"\bproduce\b", r"\bbakery\b", r"\bgrocery\b", r"\bdeli\b",
        r"\bmarket\b(?!ing)", r"\bcatering\b",
    ],
    "Healthcare & Pharma": [
        r"\bhealth\w*", r"\bmedical\b", r"\bclinic\b", r"\bpharmacy\b",
        r"\bdental\b", r"\bnursing\b",
    ],
    "E-Commerce & Fulfillment": [
        r"\bretail\b", r"\be-?commerce\b", r"\bstore\b", r"\bshop\b", r"\bboutique\b",
    ],
}


def guess_industry_tag(*texts: str | None) -> str | None:
    """Classifies free text (business name, and optionally a collateral
    description or similar) against the keyword patterns above. Checks
    each candidate tag in KEYWORD_INDUSTRY_PATTERNS's own order, first
    match wins -- order matters for a name that could plausibly hit two
    buckets (rare, but e.g. "Produce Trucking LLC" lands on Trucking &
    Logistics since it's checked first). Returns None, never a forced
    guess, when nothing matches."""
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack.strip():
        return None
    for tag, patterns in KEYWORD_INDUSTRY_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, haystack):
                return tag
    return None
