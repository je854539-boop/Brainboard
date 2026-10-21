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

from app.models.enums import CoBroker, MasterLogStatus

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
# Scoped to right after a factor-rate mention specifically ("25k@1.37 44
# weeks" is the real observed convention) rather than any bare "NN weeks"
# in the notes -- a deal TERM and an unrelated follow-up timeframe
# ("call back in 2 weeks") read identically as a bare number otherwise,
# and only the former belongs here (the latter is _RELATIVE_TIME_RE's
# job, for merchant-stated readiness, a different question entirely).
_TERM_WEEKS_RE = re.compile(r"(\d{1,3})\s*(?:wks?|weeks?)", re.IGNORECASE)


def _term_weeks_mentioned(notes: str) -> int | None:
    for rate_match in _FACTOR_RATE_RE.finditer(notes):
        term_match = _TERM_WEEKS_RE.search(notes, rate_match.end(), rate_match.end() + 25)
        if term_match:
            return int(term_match.group(1))
    return None


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
        "term_weeks_mentioned": _term_weeks_mentioned(notes),
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


# ---------------------------------------------------------------------------
# Renewal/refi eligibility -- two independent signals, deliberately not
# blended into one number: a CALCULATED projection (pure arithmetic on
# balance/payment, no text involved) and a MERCHANT-STATED one (what they
# actually told a broker on a call). The calculated one is higher-
# confidence when the inputs exist; the stated one is the only option
# when they don't, and is kept lower-confidence/labeled as such rather
# than presented with false precision.
# ---------------------------------------------------------------------------

# Payments/week per frequency -- "Daily" is a documented ASSUMPTION
# (standard ACH business-day convention, 5x/week), not a certainty: real
# lender schedules do vary (some run 7x/week) and notes alone can't say
# which for a given deal. Easy to override per-deal once that's tracked
# explicitly; until then this is the industry-standard default.
_PAYMENTS_PER_WEEK = {"weekly": 1.0, "bi-weekly": 0.5, "biweekly": 0.5, "monthly": 12 / 52, "daily": 5.0}


def project_payoff(current_balance: float | None, payment_amt: float | None, payment_freq: str | None) -> dict:
    """Pure arithmetic: at the current payment rate, how many weeks until
    this balance hits zero. Returns None fields (not a guess) when any
    required input is missing, or when payment_freq isn't one of the
    frequencies above."""
    if not current_balance or not payment_amt or not payment_freq:
        return {"weeks_to_payoff": None, "projected_payoff_date": None, "weekly_paydown_rate": None}
    per_week = _PAYMENTS_PER_WEEK.get(payment_freq.strip().lower())
    if per_week is None:
        return {"weeks_to_payoff": None, "projected_payoff_date": None, "weekly_paydown_rate": None}

    weekly_rate = payment_amt * per_week
    if weekly_rate <= 0:
        return {"weeks_to_payoff": None, "projected_payoff_date": None, "weekly_paydown_rate": weekly_rate}

    weeks = current_balance / weekly_rate
    projected_date = dt.date.today() + dt.timedelta(weeks=weeks)
    return {
        "weeks_to_payoff": round(weeks, 1),
        "projected_payoff_date": projected_date.isoformat(),
        "weekly_paydown_rate": round(weekly_rate, 2),
    }


# ASSUMPTION, same documented-default pattern as _PAYMENTS_PER_WEEK: the
# floor's working default factor rate when a call doesn't surface the
# real one. A real stated rate (see _FACTOR_RATE_RE elsewhere in this
# file) always wins when one exists -- this is only the fallback for
# reverse-engineering a missing field from a quick call.
_DEFAULT_FACTOR_RATE = 1.40


def reverse_engineer_deal_terms(
    funded_amount: float | None, payment_amt: float | None, term_weeks: float | None, factor_rate: float = _DEFAULT_FACTOR_RATE
) -> dict:
    """A call often surfaces only two of {funded amount, payment, term} --
    the relationship total_payback = funded_amount * factor_rate =
    payment_amt * term_weeks lets the missing one be solved for from
    whichever two are known, the same mental math worked on the floor
    (funded amount x 1.40, divided by payment or by term). Returns the
    field that was actually missing plus the implied total payback;
    returns everything as None, not a guess, when fewer than two of the
    three inputs are present -- there's nothing to solve for. Never
    overwrites a value that was actually given: if all three are present
    this doesn't recompute or sanity-check them (that's a different
    question -- see the balance/funded-amount mismatch flag in
    additional_position_eligibility for how a real inconsistency gets
    surfaced instead of silently picked one way)."""
    known = sum(x is not None for x in (funded_amount, payment_amt, term_weeks))
    if known < 2 or factor_rate <= 0:
        return {"funded_amount": funded_amount, "payment_amt": payment_amt, "term_weeks": term_weeks, "solved_field": None, "total_payback": None}

    if term_weeks is None:
        if not payment_amt:
            return {"funded_amount": funded_amount, "payment_amt": payment_amt, "term_weeks": None, "solved_field": None, "total_payback": None}
        total_payback = funded_amount * factor_rate
        return {"funded_amount": funded_amount, "payment_amt": payment_amt, "term_weeks": round(total_payback / payment_amt, 1), "solved_field": "term_weeks", "total_payback": round(total_payback, 2)}

    if payment_amt is None:
        if not term_weeks:
            return {"funded_amount": funded_amount, "payment_amt": None, "term_weeks": term_weeks, "solved_field": None, "total_payback": None}
        total_payback = funded_amount * factor_rate
        return {"funded_amount": funded_amount, "payment_amt": round(total_payback / term_weeks, 2), "term_weeks": term_weeks, "solved_field": "payment_amt", "total_payback": round(total_payback, 2)}

    # funded_amount missing
    total_payback = payment_amt * term_weeks
    return {"funded_amount": round(total_payback / factor_rate, 2), "payment_amt": payment_amt, "term_weeks": term_weeks, "solved_field": "funded_amount", "total_payback": round(total_payback, 2)}


_RELATIVE_TIME_RE = re.compile(r"\b(\d{1,2})\s*(?:to|-)?\s*(\d{1,2})?\s*(weeks?|months?)\b", re.IGNORECASE)
_READINESS_CONTEXT_RE = re.compile(
    r"\bpif\b|pa(?:y(?:'s|s)?|id)\s+off|ready|on\s+schedule|will\s+(?:need|use|take)|can\s+use|crack",
    re.IGNORECASE,
)


def extract_merchant_stated_readiness(notes: str | None) -> dict | None:
    """The merchant's OWN estimate of when they'll be ready -- "6 to 8
    weeks", "4-5 months" -- read from the most recent dated entry where a
    relative-time phrase co-occurs with payoff/readiness language (so
    "call back in 2 weeks" alone, with no PIF/readiness context, doesn't
    get mistaken for a payoff estimate). Later mentions supersede earlier
    ones -- a merchant revising "6-8 weeks" down to "2 weeks" later means
    the later number is what's current. Returns None if no dated entry
    has both signals -- an honest "not stated", not a guess."""
    entries = _parse_dated_entries(notes or "")
    best: dict | None = None

    for entry_date, text in entries:
        if not _READINESS_CONTEXT_RE.search(text):
            continue
        time_match = _RELATIVE_TIME_RE.search(text)
        if not time_match:
            continue
        low, high, unit = time_match.groups()
        value = (int(low) + int(high)) / 2 if high else int(low)
        days = value * 30 if unit.lower().startswith("month") else value * 7
        estimated_date = entry_date + dt.timedelta(days=days)
        if best is None or entry_date >= best["_source_entry_date"]:
            best = {
                "estimated_ready_date": estimated_date.isoformat(),
                "stated_on": entry_date.isoformat(),
                "source_text": text.strip()[:200],
                "_source_entry_date": entry_date,
            }

    if best is not None:
        del best["_source_entry_date"]
    return best


# ---------------------------------------------------------------------------
# Additional-position (stacking) eligibility -- a SEPARATE question from
# renewal/refi above: renewal asks "when does the position they already
# have clear" (genuinely paydown-gated, see project_payoff) -- stacking
# asks "can they take on a NEXT position on top of what's still open".
# Per the floor's own stated rule, stacking is NOT paydown-gated at all:
# a merchant with an existing position can be approached about another
# one immediately ("the next day"). What's actually uncertain isn't
# timing, it's the realistic AMOUNT -- each additional position funds
# for less than the last one (debt-service capacity shrinks) at a
# higher factor rate (more risk to the lender). See
# estimate_next_position_terms for the real ratios behind that.
# ---------------------------------------------------------------------------

# ASSUMPTIONS from the floor's own stated examples -- real, but thin
# (two data points: 1st->2nd ~80% of the last funded amount, 2nd->3rd
# ~70%), meant to be corrected as more examples come in. "2 points" =
# +0.02 on the factor rate, per the floor's own convention (a "40 rate"
# means 1.40). No ratio is assumed past the 3rd position -- that's past
# what's been observed, an honest "don't know" rather than extrapolating.
_NEXT_POSITION_AMOUNT_RATIO = {1: 0.80, 2: 0.70}
_NEXT_POSITION_RATE_BUMP = 0.02


def estimate_next_position_terms(
    last_funded_amount: float | None, last_payment_amt: float | None, open_positions: int | None, last_factor_rate: float | None = None
) -> dict | None:
    """What a merchant with `open_positions` existing position(s) could
    realistically be offered next -- a realistic-AMOUNT question, not a
    timing one (see the module comment above: stacking is immediate).
    Term is derived by holding the merchant's current payment roughly
    constant (their real constraint is debt-service capacity, not an
    arbitrary new schedule) and solving via reverse_engineer_deal_terms.
    Returns None when open_positions is 0/None (nothing to stack on top
    of yet) or 3+ (no real data behind a ratio that far -- see
    _NEXT_POSITION_AMOUNT_RATIO)."""
    ratio = _NEXT_POSITION_AMOUNT_RATIO.get(open_positions) if open_positions else None
    if not last_funded_amount or ratio is None:
        return None

    next_amount = round(last_funded_amount * ratio, 2)
    next_rate = round((last_factor_rate or _DEFAULT_FACTOR_RATE) + _NEXT_POSITION_RATE_BUMP, 2)
    next_term_weeks = None
    if last_payment_amt:
        next_term_weeks = reverse_engineer_deal_terms(next_amount, last_payment_amt, None, factor_rate=next_rate)["term_weeks"]

    return {
        "next_position_label": _position_label(open_positions),
        "estimated_amount": next_amount,
        "estimated_factor_rate": next_rate,
        "estimated_term_weeks": next_term_weeks,
        "basis": f"~{int(ratio * 100)}% of last funded amount, rate +2pts, term held to current payment",
    }

_ORDINAL_SUFFIXES = {1: "1st", 2: "2nd", 3: "3rd"}

_WANTS_MORE_RE = re.compile(
    r"(?<!not )(?<!n't )(?<!never )(?<!doesn't)"
    r"(?:wants?\s+(?:more|additional)|needs?\s+(?:more|additional)\s+(?:capital|money|funds?)|"
    r"asked\s+(?:about|for)\s+(?:a\s+)?(?:second|2nd|third|3rd|more)|"
    r"interested\s+in\s+(?:more|additional|stacking)|wants?\s+to\s+stack)",
    re.IGNORECASE,
)

# Caught against a real deal (Rager Construction LLC, see git history):
# "Needs 100K for project he is working on because bank draws aren't
# enough" never matched _WANTS_MORE_RE at all -- that regex only covers
# the vague "wants more/additional capital" phrasing, never a specific
# dollar figure attached to "needs"/"wants". A stated amount is a
# stronger signal than the vague phrasing (the merchant isn't just open
# to more, they named a number), so it gets its own regex, its own
# amount parser, and -- see additional_position_eligibility below --
# priority ahead of a calculated projection that hasn't happened yet.
# The negative lookahead excludes a number that's actually a duration
# ("needs 2 weeks") or a position count ("needs 2 positions"), not money.
_MONEY_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)\s*(?:(?:to|-)\s*\$?\s*([\d,]+(?:\.\d+)?))?\s*(k|K)?\b")
_CAPITAL_ASK_RE = re.compile(
    r"\b(?:needs?|wants?|looking\s+for|asking\s+for|requesting)\s+(?:an?\s+|another\s+|additional\s+)?"
    r"\$?\s*[\d,]+(?:\.\d+)?\s*k?\b"
    r"(?!\s*(?:weeks?|months?|days?|years?|positions?|payments?|%))",
    re.IGNORECASE,
)
# Same shape, opposite direction: "130K funded"/"$130,000 advance" vs
# "funded him 130K" -- real notes use both orders (see Rager's "Paypal
# 130K funded 12 months ago"). This recovers original_funded_amount
# straight from free text for the hundreds of existing leads that predate
# the tracked column, instead of leaving them with nothing to anchor
# percent-paid-down to.
_FUNDED_AMOUNT_RE = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(k|K)?\s*(?:funded|advance[d]?)\b"
    r"|(?:funded|advanced)\s+(?:him|her|them)?\s*\$?\s*([\d,]+(?:\.\d+)?)\s*(k|K)?",
    re.IGNORECASE,
)


def _parse_money(text: str) -> float | None:
    """Range-aware ("$250-300K" -> midpoint 275000) and filtered against
    the real false positives a bare small number produces -- a term
    length ("13-14 Month deal") or a position count has no $, no comma,
    no K suffix, and is under 1000, which no real floor dollar figure
    ever is (always written with a K, a comma, or a $). Returns None
    rather than guessing when the number doesn't look like money, caught
    against real notes where a bare small number was being mistaken for
    one (e.g. "wants 13-14 Month deal 100K" -> the 13-14 is a contract
    term, not money, and 100K is too far away in the text to safely
    assume it's the same figure being asked about)."""
    match = _MONEY_RE.search(text)
    if not match:
        return None
    low_str, high_str, k = match.groups()
    try:
        low = float(low_str.replace(",", ""))
        high = float(high_str.replace(",", "")) if high_str else low
    except ValueError:
        return None
    value = (low + high) / 2
    if k:
        value *= 1000
    plausible = bool(k) or "$" in match.group(0) or "," in low_str or value >= 1000
    return value if plausible else None


def _capital_ask_in(text: str) -> float | None:
    match = _CAPITAL_ASK_RE.search(text)
    return _parse_money(text[match.start():match.end() + 20]) if match else None


def extract_capital_ask(notes: str | None) -> dict | None:
    """The most recent mention of a specific dollar amount the merchant
    needs/wants -- "Needs 100K for project...", "asking for $50,000".
    Checks the UNDATED preamble (the original note, before any dated
    follow-up) as well as every dated entry -- caught against a real
    deal where _parse_dated_entries' documented behavior (text before
    the first dated entry is dropped, since there's no date to anchor it
    to) would otherwise have silently discarded the one sentence that
    actually stated the ask, just because a later, unrelated dated
    follow-up existed anywhere else in the same notes. A dated entry
    still supersedes the undated preamble when both mention a figure
    (stated_on is then that entry's date; None means it only appeared in
    the undated preamble). Returns None, not a guess, when nothing states
    an actual figure (a bare "wants more" belongs to _WANTS_MORE_RE)."""
    notes = notes or ""
    entries = _parse_dated_entries(notes)
    first_dated = _DATE_ENTRY_RE.search(notes)
    preamble = notes[: first_dated.start()] if first_dated else notes

    candidates: list[dict] = []
    preamble_amount = _capital_ask_in(preamble)
    if preamble_amount is not None:
        candidates.append({"amount": preamble_amount, "stated_on": None, "source_text": preamble.strip()[:200], "_date": dt.date.min})
    for entry_date, text in entries:
        amount = _capital_ask_in(text)
        if amount is not None:
            candidates.append({"amount": amount, "stated_on": entry_date.isoformat(), "source_text": text.strip()[:200], "_date": entry_date})

    if not candidates:
        return None
    best = max(candidates, key=lambda c: c["_date"])
    del best["_date"]
    return best


def extract_funded_amount_from_notes(notes: str | None) -> float | None:
    """Fallback for the hundreds of leads that predate
    MasterLogEntry.original_funded_amount -- a real original-amount
    mention read straight out of free text ("Paypal 130K funded 12
    months ago"), tried only when the tracked column is empty. Routes
    the matched span back through _parse_money for the same range/
    plausibility handling as extract_capital_ask -- caught against a
    real false positive: "Originally funded 12-20-25 ofr 25k@1.37" has a
    DATE right after "funded", and without this filter "12" (from
    "12-20-25") got taken as a $12 origination amount, producing a
    nonsense negative percent-paid-down once compared to a real balance.
    Returns None, not a guess, when no plausible mention exists."""
    match = _FUNDED_AMOUNT_RE.search(notes or "")
    return _parse_money(match.group(0)) if match else None


def _position_label(open_positions: int | None) -> str:
    n = (open_positions or 0) + 1
    return f"{_ORDINAL_SUFFIXES.get(n, f'{n}th')} position"


def additional_position_eligibility(
    current_balance: float | None,
    payment_amt: float | None,
    payment_freq: str | None,
    open_positions: int | None,
    notes: str | None,
    original_funded_amount: float | None = None,
) -> dict:
    """Eligibility for the NEXT stacked position (2nd, 3rd, ...), distinct
    from renewal/refi on the position already open. `basis` explains
    which signal fired: "stackable_next_day" (the merchant already has
    an existing position on file -- evidenced by a current balance, a
    known funded amount, or a tracked open_positions count -- so per the
    floor's own rule they can be approached immediately, not gated by
    paydown progress the way refi is), "capital_ask_stated" (no existing-
    position evidence, but the merchant named a specific dollar figure),
    or "merchant_stated_interest" (a vague "wants more" with no number --
    the weakest signal). eligible=False/basis=None with no date is an
    honest "not yet", not a 0.

    `original_funded_amount` (the real tracked value, see
    MasterLogEntry.original_funded_amount) is preferred whenever present
    (`paydown_source` = "structured_funded_amount"). When it isn't --
    most existing leads predate that column -- this tries
    extract_funded_amount_from_notes (a real origination-amount mention
    in free text, e.g. "130K funded 12 months ago" -- `paydown_source` =
    "notes_stated_funded_amount"), and only then falls back to treating
    the FIRST balance a dated note entry happens to mention as a proxy
    original (`paydown_source` = "notes_balance_trend"), the weakest of
    the three since that first note is often written well after
    funding, mid-paydown. All three tiers feed percent_paid_down
    (informational context), but only the top two feed estimate_next_
    position_terms's "last funded amount" -- a mid-paydown balance is
    not the original funded figure, and guessing it would understate
    what they're really good for next."""
    notes = notes or ""
    label = _position_label(open_positions)
    balance_sequence = extract_timeline(notes)["balance_sequence"]
    wants_more = bool(_WANTS_MORE_RE.search(notes))
    capital_ask = extract_capital_ask(notes)

    origin, paydown_source = None, None
    notes_funded_amount = extract_funded_amount_from_notes(notes)
    if original_funded_amount and original_funded_amount > 0:
        origin, paydown_source = original_funded_amount, "structured_funded_amount"
    elif notes_funded_amount and notes_funded_amount > 0:
        origin, paydown_source = notes_funded_amount, "notes_stated_funded_amount"
    elif len(balance_sequence) >= 2 and balance_sequence[0] > 0:
        origin, paydown_source = balance_sequence[0], "notes_balance_trend"

    percent_paid_down = None
    if origin is not None and current_balance is not None:
        percent_paid_down = round((origin - current_balance) / origin, 3)
    elif origin is not None and len(balance_sequence) >= 2:
        percent_paid_down = round((balance_sequence[0] - balance_sequence[-1]) / balance_sequence[0], 3)

    has_existing_position = bool(current_balance) or bool(origin) or bool(open_positions)
    estimated_terms, estimated_date, basis = None, None, None

    if has_existing_position:
        estimated_date, basis = dt.date.today().isoformat(), "stackable_next_day"
        if origin is not None and paydown_source != "notes_balance_trend":
            stated_rates = [float(r) for r in _FACTOR_RATE_RE.findall(notes)]
            last_factor_rate = stated_rates[-1] if stated_rates else None
            estimated_terms = estimate_next_position_terms(origin, payment_amt, open_positions, last_factor_rate)
    elif capital_ask is not None:
        estimated_date, basis = dt.date.today().isoformat(), "capital_ask_stated"
    elif wants_more:
        estimated_date, basis = dt.date.today().isoformat(), "merchant_stated_interest"
        paydown_source = None

    return {
        "eligible": basis is not None,
        "next_position_label": label,
        "basis": basis,
        "paydown_source": paydown_source,
        "percent_paid_down": percent_paid_down,
        "merchant_requested_more": wants_more,
        "capital_ask_amount": capital_ask["amount"] if capital_ask else None,
        "estimated_terms": estimated_terms,
        "estimated_date": estimated_date,
    }


def _queue_row(lead, queue_type: str, reason_category: str, estimated_date: str, source: str, detail: str) -> dict:
    return {
        "lead_uid": str(lead.lead_uid),
        "business_name": lead.business_name,
        "phone": lead.phone,
        "co_broker": lead.co_broker.value if lead.co_broker else None,
        "lender": lead.lender,
        "queue_type": queue_type,
        "reason_category": reason_category,
        "estimated_date": estimated_date,
        "source": source,
        "detail": detail,
    }


def renewal_queue(leads) -> list[dict]:
    """The living "who to call and when" queue -- every active lead's next
    funding opportunity, whichever comes first: renewal/refi on the
    position they already have, OR stacking eligibility for a NEXT
    position on top of it. Both are scanned for every lead (a deal can
    legitimately queue for both at once, e.g. close to clearing position
    1 while also already asking about a 2nd), tagged by `queue_type`
    ("renewal_refi" vs "additional_position") so the two reasons stay
    distinguishable, then merged into one soonest-first list. This is the
    mechanism for "catch deals that would fall through the cracks": it
    re-derives fresh off the sheet/calendar-sourced notes and balance
    fields on every call, so a lead nobody's actively watching still
    surfaces the moment the math or the merchant's own words say it's
    ready. `leads` is any iterable of MasterLogEntry; a lead with no
    signal on either track is left out entirely -- there's nothing to
    queue it by, not a 0 indicating it's ready now.

    Every row also carries `reason_category`, the three ways a lead can
    land here, independent of queue_type/source:
      - "merchant_requested" -- the merchant's own words (a stated dollar
        ask, a vague "wants more", or a stated readiness timeline).
      - "stacking_eligible" -- the math says a NEXT position is ready.
      - "refi_eligible" -- the math says the EXISTING position is ready
        to renew/refi.
    This is what actually answers "3 ways: merchant request, mathematical
    stacking eligibility, mathematical refi eligibility" -- queue_type/
    source already carried this distinction, reason_category just names
    it directly instead of making a caller infer it."""
    rows = []
    for lead in leads:
        calculated = project_payoff(
            float(lead.current_balance) if lead.current_balance is not None else None,
            float(lead.payment_amt) if lead.payment_amt is not None else None,
            lead.payment_freq,
        )
        if calculated["projected_payoff_date"] is not None:
            rows.append(_queue_row(
                lead, "renewal_refi", "refi_eligible", calculated["projected_payoff_date"], "calculated",
                f"{calculated['weeks_to_payoff']} weeks at current paydown rate",
            ))
        else:
            stated = extract_merchant_stated_readiness(lead.notes)
            if stated is not None:
                rows.append(_queue_row(
                    lead, "renewal_refi", "merchant_requested", stated["estimated_ready_date"], "merchant_stated",
                    f"merchant said this on {stated['stated_on']}: \"{stated['source_text']}\"",
                ))

        stacking = additional_position_eligibility(
            float(lead.current_balance) if lead.current_balance is not None else None,
            float(lead.payment_amt) if lead.payment_amt is not None else None,
            lead.payment_freq,
            lead.open_positions,
            lead.notes,
            float(lead.original_funded_amount) if lead.original_funded_amount is not None else None,
        )
        if stacking["eligible"]:
            detail = f"{stacking['next_position_label']} -- {stacking['basis']}"
            terms = stacking["estimated_terms"]
            if terms is not None:
                term_part = f", ~{terms['estimated_term_weeks']}wk" if terms["estimated_term_weeks"] is not None else ""
                detail += f" (est. ~${terms['estimated_amount']:,.0f} @ {terms['estimated_factor_rate']}{term_part})"
            if stacking["capital_ask_amount"] is not None:
                detail += f" (stated need: ~${stacking['capital_ask_amount']:,.0f})"
            if stacking["percent_paid_down"] is not None and 0 <= stacking["percent_paid_down"] <= 1:
                detail += f" ({stacking['percent_paid_down'] * 100:.0f}% paid down"
                detail += f", via {stacking['paydown_source']})" if stacking["paydown_source"] else ")"
            elif stacking["percent_paid_down"] is not None:
                # Outside [0, 1] means the balance and the origin amount
                # disagree about which is bigger -- e.g. USA Air
                # Conditioning's real sheet shows a $480K balance against
                # a $400K stated funded amount, a genuine data
                # inconsistency (additional stacking since, or a stale
                # balance), not a parsing error. Flag it plainly instead
                # of printing a nonsense negative percentage.
                detail += " (balance/funded-amount mismatch in the data -- verify)"
            is_merchant = stacking["basis"] in ("merchant_stated_interest", "capital_ask_stated")
            rows.append(_queue_row(
                lead, "additional_position", "merchant_requested" if is_merchant else "stacking_eligible",
                stacking["estimated_date"], "merchant_stated" if is_merchant else "calculated", detail,
            ))

    rows.sort(key=lambda r: r["estimated_date"])
    return rows


# ---------------------------------------------------------------------------
# Revival queue -- a SEPARATE question from everything above: not "when is
# an active deal next ready", but "this deal has drifted out of the daily
# calendar-follow-up grind -- does it still look like a real, qualified
# MCA opportunity worth calling again". Two stages, matching how this was
# actually asked for: stage 1 finds the CANDIDATES (every way a lead can
# drift -- formally stalled, an overdue follow-up nobody reset, or one
# that was never followed up at all), stage 2 is the Brain actually
# reading the lead's info/notes to decide whether it's still qualified
# before it's worth a human's time.
# ---------------------------------------------------------------------------

STALLED_STATUSES = frozenset({MasterLogStatus.DEAL_STALLED_PROXY_PASS, MasterLogStatus.GHOSTED})

# Deliberately NOT every terminal status -- Funded/Lost To Competitor/Sold
# Deal Killed/Dog Shit are resolved outcomes (someone funded, lost, or
# explicitly judged this dead), not a merchant who just stopped
# answering. Revival is about reviving momentum, not overriding a human's
# already-made call on a closed deal.
DEAD_STATUSES = frozenset(
    {MasterLogStatus.FUNDED, MasterLogStatus.LOSS_TO_COMPETITOR, MasterLogStatus.SOLD_DEAL_KILLED, MasterLogStatus.DOG_SHIT}
)

# ASSUMPTIONS, documented like the paydown threshold above -- real
# defaults driving the queue, not guaranteed "this lead is actually dead"
# calls:
_DRIFT_OVERDUE_DAYS = 14  # a follow-up date two weeks or more past due, with nothing since, counts as drifted
_DRIFT_NEVER_TOUCHED_DAYS = 14  # no follow-up ever set AND no dated note entry (or the lead itself) in 2+ weeks

# ASSUMPTION, documented like the paydown threshold above: a momentum
# score this negative means the notes are dominated by dead-deal language
# (banged it, ghosted, bust out, defaulted...) rather than a merchant who
# just got busy -- a real default, not a guaranteed "never call again".
_REVIVAL_MOMENTUM_FLOOR = -2

_DISQUALIFYING_RE = re.compile(r"no\s+business|no\s+rev(?:enue)?\b|not\s+in\s+business|out\s+of\s+business", re.IGNORECASE)


def _drift_reason(lead) -> tuple[str, int | None] | tuple[None, None]:
    """Stage 1: why (if at all) this lead counts as drifted. Checked in
    order -- a formally stalled/ghosted status is the clearest signal and
    wins outright; otherwise an overdue follow_up_date is the next most
    reliable (someone set an explicit date and it passed); only when
    there's no follow_up_date at all does this fall back to the notes'
    own dated-entry history (extract_timeline), and finally to the lead's
    created_at age for a lead with no dated entries either. A lead with a
    current/future follow_up_date is correctly NOT a candidate here --
    it's already on the calendar grind, which is exactly the thing this
    queue is meant to complement, not duplicate.

    "follow_up_stale_vs_notes" is a DATA-HYGIENE alert, not a real drift
    case: an overdue follow_up_date, but a dated note entry exists AFTER
    that date -- someone already got this merchant on the phone past the
    date that was supposed to mean "next contact", the field just was
    never moved forward to match. This is exactly the case that would
    otherwise slip through silently: the lead LOOKS drifted by the
    numbers, but a human already acted and the data needs rectifying,
    not a revival call."""
    now = dt.datetime.now(dt.timezone.utc)
    timeline = extract_timeline(lead.notes or "")
    last_note_date = dt.date.fromisoformat(timeline["last_entry_date"]) if timeline["last_entry_date"] else None

    if lead.status in STALLED_STATUSES:
        return "stalled_status", None

    if lead.follow_up_date is not None:
        days_overdue = (now - lead.follow_up_date).days
        if days_overdue < _DRIFT_OVERDUE_DAYS:
            return None, None
        if last_note_date is not None and last_note_date > lead.follow_up_date.date():
            return "follow_up_stale_vs_notes", days_overdue
        return "overdue_follow_up", days_overdue

    days_quiet = timeline["days_since_last_entry"]
    if days_quiet is None and lead.created_at is not None:
        days_quiet = (now - lead.created_at).days
    return ("never_followed_up", days_quiet) if days_quiet is not None and days_quiet >= _DRIFT_NEVER_TOUCHED_DAYS else (None, None)


def revival_queue(leads, min_revenue: float | None = None) -> list[dict]:
    """Every lead that's drifted out of the daily calendar-follow-up
    grind (see _drift_reason) AND still looks like a real, qualified MCA
    opportunity based on its own data -- built to "catch deals that went
    quiet but shouldn't have been given up on", whether or not anyone
    ever formally marked them stalled. Excludes a lead outright when its
    notes explicitly say there's no real business/revenue behind it (a
    real example read off the calendar: "Dale Bragg - No Business (No
    Rev)"), or when `min_revenue` is given and either annual_revenue is
    missing or under it, or when the floor-language momentum score is
    too deep in dead-deal territory (see _REVIVAL_MOMENTUM_FLOOR).
    `leads` should already exclude DEAD_STATUSES by the caller -- this
    doesn't re-check that itself, since a Funded/Lost/Killed/Dog-Shit
    lead passed in here is a caller bug, not something to silently
    filter twice."""
    rows = []
    for lead in leads:
        reason, days = _drift_reason(lead)
        if reason is None:
            continue
        if _DISQUALIFYING_RE.search(lead.notes or ""):
            continue
        if min_revenue is not None and (lead.annual_revenue is None or lead.annual_revenue < min_revenue):
            continue

        signals = extract_signals(lead.business_name, lead.notes)
        if signals["momentum_score"] <= _REVIVAL_MOMENTUM_FLOOR:
            continue

        detail = {
            "stalled_status": f"marked {lead.status.value}",
            "overdue_follow_up": f"follow-up was {days}d overdue, nothing since",
            "never_followed_up": f"never followed up, quiet {days}d",
            "follow_up_stale_vs_notes": "RECTIFY: notes show contact after the follow-up date -- update the follow-up date/status, not a cold lead",
        }[reason]
        rows.append({
            "lead_uid": str(lead.lead_uid),
            "business_name": lead.business_name,
            "phone": lead.phone,
            "co_broker": lead.co_broker.value if lead.co_broker else None,
            "lender": lead.lender,
            "status": lead.status.value,
            "annual_revenue": lead.annual_revenue,
            "industry": signals["industry"],
            "momentum_score": signals["momentum_score"],
            "drift_reason": reason,
            "days_drifted": days,
            "detail": f"{detail}, momentum {signals['momentum_score']:+d}",
        })

    rows.sort(key=lambda r: r["days_drifted"] if r["days_drifted"] is not None else -1, reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Hot-leads queue -- a THIRD, distinct question from the other two: not
# "this deal went quiet, is it still worth reviving" (revival_queue) and
# not "this deal funded, is it ready for a stack/refi" (renewal_queue) --
# "this deal is still in the normal daily chase, but the notes say NOW is
# a particularly good moment to call, ahead of whatever date is already
# on the calendar". Deliberately built entirely from the SAME floor-
# language signals used everywhere else (never a new, hidden scoring
# model) -- every reason here is legible and traceable to real note
# text, same rule as the rest of this file.
# ---------------------------------------------------------------------------

_HOT_RECENT_DAYS = 3  # ASSUMPTION: a signal this fresh is worth a same-week nudge, not a guaranteed "call today or lose it"


def _days_ago(iso_date: str) -> str:
    days = (dt.date.today() - dt.date.fromisoformat(iso_date)).days
    return "today" if days <= 0 else f"{days}d ago"


def hot_leads_queue(leads) -> list[dict]:
    """Leads still in the normal daily chase (caller should pass leads
    already excluding STALLED_STATUSES/DEAD_STATUSES -- a stalled lead
    belongs to revival_queue, a funded one to renewal_queue, not here)
    where recent notes suggest THIS WEEK is a better moment to call than
    whatever's already on the calendar. Each lead can carry multiple
    independent reasons (never blended into one score):
      - "warming_momentum": the timeline's momentum_trend (recent entries
        vs earlier ones) is positive -- the deal is heating up.
      - "fresh_capital_ask": a dollar-amount ask was stated within
        _HOT_RECENT_DAYS.
      - "fresh_readiness_statement": a merchant-stated readiness timeline
        was given within _HOT_RECENT_DAYS.
      - "recent_positive_touch": the most recent dated entry is within
        _HOT_RECENT_DAYS AND reads net-positive (momentum_score > 0).
    A lead with none of these is left out entirely -- no reason to flag
    it ahead of its normal follow-up date."""
    rows = []
    for lead in leads:
        notes = lead.notes or ""
        timeline = extract_timeline(notes)
        signals = extract_signals(lead.business_name, notes)
        capital_ask = extract_capital_ask(notes)
        readiness = extract_merchant_stated_readiness(notes)
        today = dt.date.today()

        reasons: list[tuple[str, str]] = []
        if timeline["entry_count"] >= 2 and timeline["momentum_trend"] > 0:
            reasons.append(("warming_momentum", f"momentum trending up ({timeline['momentum_trend']:+.1f})"))
        if capital_ask and capital_ask["stated_on"] and (today - dt.date.fromisoformat(capital_ask["stated_on"])).days <= _HOT_RECENT_DAYS:
            reasons.append(("fresh_capital_ask", f"asked for ~${capital_ask['amount']:,.0f} {_days_ago(capital_ask['stated_on'])}"))
        if readiness and (today - dt.date.fromisoformat(readiness["stated_on"])).days <= _HOT_RECENT_DAYS:
            reasons.append(("fresh_readiness_statement", f"gave a readiness timeline {_days_ago(readiness['stated_on'])}"))
        if (
            timeline["days_since_last_entry"] is not None
            and timeline["days_since_last_entry"] <= _HOT_RECENT_DAYS
            and signals["momentum_score"] > 0
        ):
            reasons.append(("recent_positive_touch", f"positive note {timeline['days_since_last_entry']}d ago"))

        if not reasons:
            continue

        rows.append({
            "lead_uid": str(lead.lead_uid),
            "business_name": lead.business_name,
            "phone": lead.phone,
            "co_broker": lead.co_broker.value if lead.co_broker else None,
            "status": lead.status.value,
            "hot_reasons": [r for r, _ in reasons],
            "days_since_last_entry": timeline["days_since_last_entry"],
            "detail": "; ".join(d for _, d in reasons),
        })

    rows.sort(key=lambda r: r["days_since_last_entry"] if r["days_since_last_entry"] is not None else 999)
    return rows
