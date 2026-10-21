"""Extracts structured signal from a lead's free-text notes, using a
lexicon built from actually reading hundreds of real notes/calendar
entries from this floor -- not a generic sentiment/NLP dictionary.
Deterministic string/regex matching only: no external API calls, no
model inference, nothing leaves this process. This is the "floor
intelligence" track -- human-observed deal momentum and language that no
enrichment API can ever see -- complementing, not replacing, the
"verified facts" track (Apollo/Cobalt/SOS) built elsewhere.

Every list below is seeded from real examples read this session and is
expected to need extending over time as the floor's language evolves --
that's a lexicon edit, not a retrain, which is the point of this
approach over a black-box model.
"""

import datetime as dt
import re

from app.models.enums import CoBroker

# Nickname/shorthand variants actually observed in real notes this
# session, mapped to the canonical CoBroker each one really refers to.
# Deliberately NOT exhaustive -- a bare first name (e.g. "Nick", "Mike")
# is matched because the current 20-person roster only has one Nick F
# and one Mike F, not because that's a safe assumption in general; if a
# merchant happens to share a broker's first name, this can misfire.
_BROKER_ALIASES: dict[str, CoBroker] = {
    "nick f": CoBroker.NICK_F, "nick": CoBroker.NICK_F,
    "mike f": CoBroker.MIKE_F, "mike falcone": CoBroker.MIKE_F, "mike": CoBroker.MIKE_F,
    "vinny": CoBroker.VINNY, "vinnie": CoBroker.VINNY,
    "victor": CoBroker.VICTOR,
    "gallo": CoBroker.GALLO,
    "shaun": CoBroker.SHAUN,
    "kris with a k": CoBroker.KRIS, "kris": CoBroker.KRIS,
    "danstol": CoBroker.DAN_STOL, "dan stol": CoBroker.DAN_STOL, "dan b": CoBroker.DAN_STOL,
    "boggie": CoBroker.DAN_STOL, "bogie": CoBroker.DAN_STOL,
    "roman": CoBroker.ROMAN,
    "james": CoBroker.JAMES,
    "alfred": CoBroker.ALFRED,
    "marcus": CoBroker.MARCUS,
    "ricky": CoBroker.RICKY,
    "zack": CoBroker.ZACK,
    "emilio": CoBroker.EMILIO,
    "boula": CoBroker.BOULA,
    "ariel": CoBroker.ARIEL,
    "tony": CoBroker.TONY,
    "max": CoBroker.MAX,
    "sebastian": CoBroker.SEBASTIAN, "seb": CoBroker.SEBASTIAN, "sebas": CoBroker.SEBASTIAN,
    "sea-bass": CoBroker.SEBASTIAN, "seabass": CoBroker.SEBASTIAN, "sea bass": CoBroker.SEBASTIAN,
    "bass": CoBroker.SEBASTIAN,
}
# Longest alias first so "kris with a k" matches before the bare "kris"
# inside it does, "dan b" before "dan stol", etc.
_BROKER_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in sorted(_BROKER_ALIASES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Lenders actually named in real notes this session. Matched case-
# insensitively as whole words/phrases so "On Deck"/"OnDeck" both hit.
_KNOWN_LENDERS = [
    "OnDeck", "On Deck", "Rapid Advance", "Rapid", "Kapitus", "Fundbox", "Credibly",
    "Forward", "Headway", "Itria", "Balboa", "Can Capital", "Avalon", "Expansion",
    "Fenix", "Quantum", "Essentia", "Samson", "Specialty", "Bluevine", "FDM",
    "Clearview", "Litefund", "Zen", "B2B Puerto Rico", "Mulligan", "Velocity",
    "Bizfund", "Biz Fund", "Surge", "Epic", "DLP", "Square", "Honey Funding", "Vox",
    "QFS", "TVT", "Newco", "Swift", "EBF", "Vader", "PG Solutions", "Alternative",
    "Flo", "App Funding", "McKenzie", "Rowan", "Rowen", "LG", "Kalamata", "Fox",
    "InAdvance", "Byzfunder", "Fintegra", "Lite Fund",
]

# Industry inference from business_name/notes keywords -- real business
# names observed this session, not an abstract taxonomy.
_INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "Construction/Contracting": ["construction", "contractor", "contracting", "builders", "builder", "remodel", "masonry", "roofing", "concrete", "drywall", "framing", "carpentry"],
    "Restaurant/Food Service": ["restaurant", "taco", "pizza", "bar", "bakery", "grill", "cafe", "diner", "bbq", "kitchen", "food"],
    "Trucking/Transportation/Logistics": ["trucking", "transport", "logistics", "freight", "taxi", "dispatch", "fleet"],
    "Auto/Repair/Sales": ["auto", "automotive", "mechanic", "tire", "motors", "car sales", "collision", "body shop"],
    "Retail/Grocery": ["market", "grocery", "supermarket", "store", "variety", "outlet", "supply company"],
    "Medical/Healthcare": ["medical", "dental", "dds", "md", "clinic", "pharmacy", "physical therapy", "health"],
    "Agriculture/Farming": ["farm", "farming", "grain", "produce", "ranch", "grove", "dairy", "cattle"],
    "Landscaping/Tree Service": ["landscape", "landscaping", "tree care", "tree service", "lawn"],
    "Electrical/Plumbing/HVAC": ["electrical", "electric", "plumbing", "hvac", "heating and", "refrigeration"],
    "Beauty/Personal Care": ["salon", "spa", "beauty", "barber"],
    "Childcare/Education": ["day care", "daycare", "academy", "child care", "early ed"],
    "Manufacturing": ["manufacturing", "fabrication", "machining", "metals", "steel", "tooling"],
    "Real Estate/Property": ["realty", "real estate", "property group", "holdings"],
    "Security/Cleaning Services": ["security service", "cleaning", "janitorial"],
    "Entertainment/Events": ["events", "entertainment", "productions"],
}

# Floor vocabulary, read directly out of real notes -- not a generic
# sentiment lexicon. Momentum is intentionally coarse (sum of hits, not a
# calibrated probability); it's meant to flag which leads are warming up
# vs. going cold for a human to glance at, not to replace judgment.
_POSITIVE_PHRASES = [
    "cracked", "docs in", "sent docs", "signed", "app sent", "down for it",
    "sounds good", "said yes", "gave up the email", "statements are in",
    "funded", "submitted", "going to send", "will have docs",
]
_NEGATIVE_PHRASES = [
    "banged it", "placating", "ghosted", "no answer", " na ", "stvm", "capping",
    "went ghost", "hung up", "blocking", "bust out", "default", "toast",
    "deal is trash", "dog shit", "mutt", "cocksucker",  # the floor's own words for a dead/bad deal, used as a dead-deal signal, not endorsed language
]

_PIF_RE = re.compile(r"\bPIF\b|paid\s+(it\s+)?off|paid\s+in\s+full", re.IGNORECASE)
_FACTOR_RATE_RE = re.compile(r"@\s*(1\.\d{1,2})")
_LEAD_SOURCE_RE = re.compile(r"Lead\s+Source[: \t]*([A-Za-z0-9 &+/-]+)", re.IGNORECASE)
_STACKING_RE = re.compile(r"\b(\d)\s*(?:positions?|poitions?)\b", re.IGNORECASE)


def _find_lenders(text: str) -> list[str]:
    found = []
    lower = text.lower()
    for lender in _KNOWN_LENDERS:
        if lender.lower() in lower and lender not in found:
            found.append(lender)
    return found


def _infer_industry(business_name: str, notes: str) -> str | None:
    haystack = f"{business_name} {notes}".lower()
    for industry, keywords in _INDUSTRY_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            return industry
    return None


def extract_signals(business_name: str, notes: str | None) -> dict:
    """Returns a dict of everything this lexicon can pull out of one
    lead's notes. Every field is None/empty/0 rather than a guess when
    nothing matched -- "not detected" is a real, honest answer here, not
    a gap to paper over."""
    notes = notes or ""

    lead_source_match = _LEAD_SOURCE_RE.search(notes)
    factor_rates = [float(m) for m in _FACTOR_RATE_RE.findall(notes)]
    stacking_matches = [int(m) for m in _STACKING_RE.findall(notes)]

    positive_hits = sum(1 for phrase in _POSITIVE_PHRASES if phrase in notes.lower())
    negative_hits = sum(1 for phrase in _NEGATIVE_PHRASES if phrase in notes.lower())

    return {
        "industry": _infer_industry(business_name, notes),
        "lead_source": lead_source_match.group(1).strip() if lead_source_match else None,
        "mentioned_lenders": _find_lenders(notes),
        "factor_rates_mentioned": factor_rates,
        "recent_payoff_detected": bool(_PIF_RE.search(notes)),
        "stacking_count_mentioned": max(stacking_matches) if stacking_matches else None,
        "momentum_score": positive_hits - negative_hits,
        "momentum_positive_hits": positive_hits,
        "momentum_negative_hits": negative_hits,
    }


# ---------------------------------------------------------------------------
# Timeline/trajectory extraction -- notes aren't one blob, they're a dated
# call-by-call log appended to over weeks or months (confirmed by reading
# real examples: "6/10/26: ...", blank line, "6/17/26: ...", etc.). A
# single aggregate momentum score over the whole blob can't tell a deal
# that started hot and is cooling apart from one that started lukewarm and
# is warming up -- same number, opposite reality. This splits notes into
# their real dated entries and scores the TREND, the same instinct behind
# the Cox time-varying model in hazard_engine.py applied to this signal.
# ---------------------------------------------------------------------------

_DATE_ENTRY_RE = re.compile(r"(?:^|\n)[ \t]*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?[ \t]*[:;]?[ \t]*", re.MULTILINE)


def _resolve_year(raw_year: str | None, carry_forward: int) -> int:
    if raw_year is None:
        return carry_forward
    year = int(raw_year)
    return year + 2000 if year < 100 else year


def _parse_dated_entries(notes: str) -> list[tuple[dt.date, str]]:
    """Splits notes into (date, entry_text) pairs in the order they
    appear -- confirmed by reading real notes to be chronological (each
    new call gets appended at the end). Text before the first dated
    entry (the original, undated note) is not included -- there's no
    date to anchor it to. A date that doesn't actually exist (a real
    observed typo, e.g. "9/290/26") is skipped rather than crashing the
    whole parse."""
    matches = list(_DATE_ENTRY_RE.finditer(notes))
    entries: list[tuple[dt.date, str]] = []
    carry_forward_year = dt.date.today().year

    for i, match in enumerate(matches):
        month, day, raw_year = match.groups()
        year = _resolve_year(raw_year, carry_forward_year)
        carry_forward_year = year
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(notes)
        body = notes[body_start:body_end].strip()
        try:
            entry_date = dt.date(year, int(month), int(day))
        except ValueError:
            continue
        entries.append((entry_date, body))

    return entries


def _entry_momentum(text: str) -> int:
    lower = text.lower()
    return sum(1 for p in _POSITIVE_PHRASES if p in lower) - sum(1 for p in _NEGATIVE_PHRASES if p in lower)


def _find_people(text: str) -> list[str]:
    """Distinct co-brokers named in this text, in order of first mention,
    resolved from the real nickname variants in _BROKER_ALIASES (e.g.
    "Dan B got him on the line and passed him to me and then i passed to
    Shaun" -> ["DanStol", "Shaun"])."""
    seen: list[str] = []
    for match in _BROKER_ALIAS_RE.finditer(text):
        canonical = _BROKER_ALIASES[match.group(1).lower()].value
        if canonical not in seen:
            seen.append(canonical)
    return seen


_PASSED_TO_RE = re.compile(
    r"(?:pass(?:ed)?|headset\s+pass|proxy\s+pass)[^.\n]{0,20}?\bto\b[^.\n]{0,25}?\b("
    + "|".join(re.escape(a) for a in sorted(_BROKER_ALIASES, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)

_DOCS_IN_RE = re.compile(r"docs\s+(?:are\s+)?in|sent\s+docs|docs\s+and\s+app\s+in|submitted", re.IGNORECASE)
_NEW_ADVANCE_RE = re.compile(r"\btook\s+(?:another\s+)?\$?[\d,]+\s*k?\b|picked\s+up|stacked|another\s+position", re.IGNORECASE)
_BALANCE_RE = re.compile(r"balance[^.\n\d]{0,20}\$?([\d,]+)\s*k?", re.IGNORECASE)


def _find_handoffs(text: str) -> list[str]:
    """Who this entry says the call was passed TO -- the handoff chain,
    not just who's named. "Dan B got him on the line and passed him to
    me and then i passed to Shaun" -> ["Shaun"] (the speaker, "me", isn't
    resolvable to a name from text alone, so only the named recipient is
    captured)."""
    return [_BROKER_ALIASES[m.group(1).lower()].value for m in _PASSED_TO_RE.finditer(text)]


def extract_timeline(notes: str | None) -> dict:
    """The trajectory view: entry_count, touch cadence, days since last
    contact, and momentum_trend (recent entries' average momentum minus
    earlier entries' -- positive means warming up, negative means
    cooling off, 0.0 with fewer than 2 dated entries means there isn't
    enough history yet to call a trend, not that it's flat). Honest
    limitation: this only sees entries the floor actually dated: a note
    with no dates in it, or a date typo'd badly enough to not parse,
    contributes nothing to the timeline even though the text is real."""
    notes = notes or ""
    entries = _parse_dated_entries(notes)
    # People/handoffs/docs-in attribution still count undated header text
    # (the original note) -- a deal can be "touched" by whoever opened it
    # even before any dated follow-up exists.
    all_people = _find_people(notes)

    if not entries:
        return {
            "entry_count": 0,
            "first_entry_date": None,
            "last_entry_date": None,
            "days_since_last_entry": None,
            "avg_days_between_entries": None,
            "momentum_trend": 0.0,
            "distinct_people_touched": all_people,
            "touch_count_people": len(all_people),
            "handoff_chain": _find_handoffs(notes),
            "docs_in_attributed_to": [],
            "balance_trend": None,
            "balance_sequence": [],
            "new_advance_detected": bool(_NEW_ADVANCE_RE.search(notes)),
        }

    entries.sort(key=lambda e: e[0])
    dates = [e[0] for e in entries]
    texts = [text for _, text in entries]
    momentums = [_entry_momentum(text) for text in texts]

    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    avg_gap = round(sum(gaps) / len(gaps), 1) if gaps else None

    if len(momentums) >= 2:
        split = max(1, len(momentums) // 2)
        earlier_avg = sum(momentums[:split]) / split
        recent_avg = sum(momentums[split:]) / (len(momentums) - split)
        trend = round(recent_avg - earlier_avg, 2)
    else:
        trend = 0.0

    handoff_chain: list[str] = []
    for text in texts:
        handoff_chain.extend(_find_handoffs(text))

    docs_in_people: list[str] = []
    for text in texts:
        if _DOCS_IN_RE.search(text):
            for person in _find_people(text):
                if person not in docs_in_people:
                    docs_in_people.append(person)

    balance_sequence: list[float] = []
    for text in texts:
        m = _BALANCE_RE.search(text)
        if m:
            try:
                balance_sequence.append(float(m.group(1).replace(",", "")))
            except ValueError:
                pass
    if len(balance_sequence) >= 2:
        balance_trend = "decreasing" if balance_sequence[-1] < balance_sequence[0] else (
            "increasing" if balance_sequence[-1] > balance_sequence[0] else "flat"
        )
    else:
        balance_trend = None

    return {
        "entry_count": len(entries),
        "first_entry_date": dates[0].isoformat(),
        "last_entry_date": dates[-1].isoformat(),
        "days_since_last_entry": (dt.date.today() - dates[-1]).days,
        "avg_days_between_entries": avg_gap,
        "momentum_trend": trend,
        "distinct_people_touched": all_people,
        "touch_count_people": len(all_people),
        "handoff_chain": handoff_chain,
        "docs_in_attributed_to": docs_in_people,
        "balance_trend": balance_trend,
        "balance_sequence": balance_sequence,
        "new_advance_detected": bool(_NEW_ADVANCE_RE.search(notes)),
    }
