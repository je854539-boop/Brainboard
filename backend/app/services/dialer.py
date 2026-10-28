"""SignalWire dialer orchestration: builds a campaign's dial queue from
calendar leads / certain Sheet leads / silo-assigned candidates, picks a
local-presence caller-ID number, places outbound calls via
services/signalwire_adapter.py, and applies the human purge/advance
disposition through the *existing* pipeline mutations (pipeline.py) --
exactly the same code path the manual silo Convert/Dismiss buttons use, so
Sheet push / Calendar sync / activity logging all fire normally regardless
of whether the mutation came from a click or a call outcome.

Phase 1 scope only: outbound calling, local-presence number selection,
manual per-call disposition. Explicitly NOT built here (deferred to a
follow-up phase on a fresh request): live patch-in/double-dial
conferencing (bridging the user's own in-progress "company dialer" call to
a live-answered lead), SMS/10DLC, and a real predictive volume-pacing
algorithm -- `run_campaign`'s `max_calls` is a manual batch-size cap, not
automatic pacing.

Inbound (ring-group) IS built here, on a fresh follow-up request -- see
routers/dialer.py's /webhooks/inbound. HONESTY NOTE on the "which phone
answered" mechanism: a simultaneous multi-<Number> <Dial> is standard,
well-documented Twilio-compatible LaML, and giving each <Number> its own
statusCallback is the standard documented way to identify which leg
connected (the other legs' callbacks report no-answer/canceled, never
in-progress). SignalWire advertises Twilio REST/LaML compatibility, so
this should carry over -- but this sandbox cannot reach signalwire.com to
confirm that specific behavior against their live docs, unlike CWMS
(verified earlier this project against USACE's own open-source client).
Smoke-test this against a real SignalWire account before trusting
attribution in production; the rest of the ring-group flow (caller
lookup, LaML structure, credit ledger) doesn't depend on that assumption
being right and is fully verified here.
"""

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import (
    ActivitySource,
    CoBroker,
    DialerCallDirection,
    DialerCallStatus,
    DialerDisposition,
    LeadContributionReason,
    MasterLogStatus,
    SiloCandidateStatus,
    TelemetrySource,
)
from app.models.orm import (
    DialerCallAttempt,
    DialerCampaign,
    DialerNumber,
    DialerNumberPool,
    InboundRingTarget,
    LeadContributor,
    MasterLogEntry,
    SiloCandidate,
)
from app.services import call_analysis, pipeline
from app.services.signalwire_adapter import SignalWireAdapter, get_signalwire_adapter

logger = logging.getLogger("brainboard.dialer")

# Hard ceiling on any one campaign's dial queue -- see DialerCampaign.max_list_size.
# Never relaxable above this from a request payload; keeps any single list
# campaign well under the scale that invites TCPA/mass-calling exposure.
HARD_MAX_LIST_SIZE = 1000


@dataclass
class DialQueueEntry:
    lead_source: str  # "calendar" | "sheet_lead" | "silo_candidate"
    entity_uid: uuid.UUID
    company_name: str
    to_number: str | None
    attempts_so_far: int


def _area_code(phone: str | None) -> str | None:
    """Best-effort US/CA area code extraction from an E.164-ish number
    (e.g. "+19175551234" -> "917"). Returns None for anything that doesn't
    look like an 11-digit NANP number rather than guessing."""
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:4]
    if len(digits) == 10:
        return digits[:3]
    return None


def _last10_digits(phone: str | None) -> str | None:
    """Normalizes any NANP phone representation to its bare last-10-digit
    form so formats can be compared regardless of source -- Master Log
    stores "(917) 555-1234", SignalWire's inbound Caller ID arrives as
    E.164 "+19175551234". Returns None if there aren't at least 10
    digits, rather than matching on a partial/garbage number."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else None


def identify_inbound_caller(db: Session, from_number: str) -> tuple[str | None, uuid.UUID | None, str | None]:
    """Matches an inbound caller's number against known leads first, then
    silo candidates, by normalized last-10-digits (see _last10_digits) --
    a full-table scan-and-compare rather than a SQL-side digit-strip,
    since lead volume here doesn't warrant the extra index/computed-column
    complexity. Returns (kind, entity_uid, company_name); (None, None,
    None) for an unrecognized number -- not an error, just an unknown
    caller who gets ring-grouped like anyone else without pre-fill."""
    target = _last10_digits(from_number)
    if target is None:
        return None, None, None

    for lead in db.execute(select(MasterLogEntry.lead_uid, MasterLogEntry.phone, MasterLogEntry.business_name)).all():
        if _last10_digits(lead.phone) == target:
            return "lead", lead.lead_uid, lead.business_name

    for candidate in db.execute(
        select(SiloCandidate.candidate_uid, SiloCandidate.phone, SiloCandidate.company_name)
    ).all():
        if _last10_digits(candidate.phone) == target:
            return "silo_candidate", candidate.candidate_uid, candidate.company_name

    return None, None, None


# Safe, curated TTS phrases per TelemetrySource -- deliberately NOT a
# pull from the entity's own notes field. Notes carry internal broker
# shorthand, disqualifying flags, and candid commentary never meant for
# an external caller to hear read aloud; this is a fixed, reviewed
# mapping instead, same "vetted copy, not raw internal data" posture as
# INDUSTRY_INBOUND_GREETINGS. Sources with no real "why we reached out"
# meaning (DEEPGRAM_NOVA, INTERZOID, DRIVE_OCR -- enrichment/data-quality
# adapters, not lead-generating signals) are deliberately absent, same
# "None means fall through" contract as industry_greeting_for_number.
SIGNAL_PHRASES: dict[TelemetrySource, str] = {
    TelemetrySource.CME_GLOBEX: "recent commodity price movement",
    TelemetrySource.FRED: "current credit market conditions",
    TelemetrySource.GDELT: "recent trade and tariff news",
    TelemetrySource.IMPORT_GENIUS: "your recent import activity",
    TelemetrySource.UCC_FILINGS: "a recent UCC filing on file",
    TelemetrySource.UCC_BATCH_UPLOAD: "a recent UCC filing on file",
    TelemetrySource.OPENFDA: "a recent FDA recall notice",
    TelemetrySource.SONAR: "current trucking capacity conditions",
    TelemetrySource.USACE: "recent river and port activity",
    TelemetrySource.NOAA_NWS: "a recent weather disruption in your area",
    TelemetrySource.SOS_REGISTRIES: "your business registration status",
    TelemetrySource.REGRID: "your property and facility records",
    TelemetrySource.COBALT_INTELLIGENCE: "your business standing records",
    TelemetrySource.APOLLO: "your company profile",
    TelemetrySource.TRANSMETRIQ: "recent rail shipment activity",
    TelemetrySource.PLANET_LABS: "recent facility activity",
    TelemetrySource.SENTINEL_HUB: "recent facility activity",
    TelemetrySource.XENETA: "current air freight rate conditions",
    TelemetrySource.HIGHERGOV: "your government contract activity",
    TelemetrySource.DATALASTIC: "recent vessel activity",
    TelemetrySource.VESSELFINDER: "recent vessel activity",
    TelemetrySource.GFW_4WINGS: "recent vessel activity",
    TelemetrySource.ADSB: "recent air cargo activity",
    TelemetrySource.RAIL_TRAFFIC: "rail traffic conditions",
    TelemetrySource.FMCSA: "your fleet's registration records",
}


def personalized_greeting_for_caller(db: Session, kind: str | None, entity_uid: uuid.UUID | None, company_name: str | None) -> str | None:
    """The real "Dynamic Signal Greeting" -- builds on identify_inbound_
    caller's existing ANI match instead of re-doing it. Looks up the
    matched entity's own source_silo/source_channel (MasterLogEntry) or
    silo/origin_source (SiloCandidate) and, when a SIGNAL_PHRASES entry
    exists for it, names the real reason this specific caller was
    contacted. Falls back to a company-name-only greeting when the
    source has no mapped phrase, and returns None entirely for an
    unidentified caller or missing company name -- the inbound handler
    treats None as "fall through to the industry-pool greeting, then the
    fully generic flow," same priority chain as before this existed."""
    if not kind or entity_uid is None or not company_name:
        return None

    source: TelemetrySource | None = None
    if kind == "lead":
        entry = db.get(MasterLogEntry, entity_uid)
        source = entry.source_channel if entry else None
    elif kind == "silo_candidate":
        candidate = db.get(SiloCandidate, entity_uid)
        source = candidate.origin_source if candidate else None

    phrase = SIGNAL_PHRASES.get(source) if source else None
    if phrase:
        return f"Thanks for calling back regarding {phrase} for {company_name}. Connecting you with someone who can help right now."
    return f"Thanks for calling back, {company_name}. Connecting you now."


# Inbound TTS greeting per DialerNumberPool.industry_tag -- played via
# <Say> before the existing ring-group <Dial>, so a merchant calling back
# a number dedicated to one UCC Batch Silo industry hears a greeting that
# names their own situation, then falls into the exact same "whoever
# picks up first" roster as every other inbound call. Deliberately plain
# TTS, not a recorded human voice -- that's a different, lower-stakes
# category than pitch_recording_url's real-recording-only pitch
# (outbound_laml's docstring/the product decision behind it): this is a
# neutral routing greeting, not a synthetic sales pitch standing in for
# a broker. Reasoned copy, not measured -- edit freely, same posture as
# every other string template in this project. Tag strings deliberately
# match the SiloName values they correspond to for a clean mental model,
# though industry_tag itself is free text on the pool, not typed against
# SiloName -- a pool can carry any tag, unmatched tags just skip the
# greeting and fall through to the existing generic flow untouched.
INDUSTRY_INBOUND_GREETINGS: dict[str, str] = {
    "Trucking & Logistics": "Thanks for calling about fleet or equipment financing. Connecting you with someone who can help right now.",
    "Food Processing & Fabrication": "Thanks for calling about working capital for your business. Connecting you now.",
    "Heavy Machinery & Industrial Equipment": "Thanks for calling about equipment financing. Connecting you now.",
    # Every other real industry category this build actually works --
    # matches SiloName's own labels (minus "Silo"/"Funnel") so there's a
    # tag available for every live silo, not just the UCC Batch subset.
    "Tariff": "Thanks for calling about trade and tariff-related financing. Connecting you now.",
    "Oil & Gas / Refining": "Thanks for calling about working capital for your operation. Connecting you with someone who can help right now.",
    "Agriculture & Grain Handling": "Thanks for calling about financing for your operation. Connecting you now.",
    "Healthcare & Pharma": "Thanks for calling about working capital for your practice or business. Connecting you now.",
    "E-Commerce & Fulfillment": "Thanks for calling about working capital for your business. Connecting you now.",
    "HigherGov": "Thanks for calling about financing against your government contract work. Connecting you now.",
}


def industry_greeting_for_number(db: Session, to_number: str | None) -> str | None:
    """Looks up which DialerNumber was actually called (inbound webhook's
    `To` field), then that number's pool's industry_tag, then the
    matching greeting above. Returns None (not a guess) for an
    unregistered number, a pool with no industry_tag, or an unmatched
    tag -- the inbound handler treats None as "use the existing
    generic flow," so every number that predates this feature keeps
    behaving exactly as it did before."""
    target = _last10_digits(to_number)
    if target is None:
        return None
    for number in db.execute(select(DialerNumber)).scalars().all():
        if _last10_digits(number.phone_number) != target:
            continue
        pool = db.get(DialerNumberPool, number.pool_id) if number.pool_id else None
        if pool is None or not pool.industry_tag:
            return None
        return INDUSTRY_INBOUND_GREETINGS.get(pool.industry_tag)
    return None


def active_ring_targets(db: Session) -> list[InboundRingTarget]:
    return db.execute(
        select(InboundRingTarget).where(InboundRingTarget.is_active.is_(True)).order_by(InboundRingTarget.created_at)
    ).scalars().all()


def credit_contributor(
    db: Session, lead_uid: uuid.UUID, co_broker: CoBroker, reason: LeadContributionReason
) -> LeadContributor | None:
    """Additive credit only -- per the explicit product decision this was
    built from ("whoever answers gets added to the deal, there's enough $
    to go around"), never replaces MasterLogEntry.co_broker. Returns None
    (no-op, not an error) if co_broker is already the lead's primary
    assignment -- crediting someone for a lead they already own would
    just be noise -- or if this exact (lead, broker, reason) credit
    already exists, so re-processing a webhook retry can't double-credit
    the same pickup."""
    lead = db.get(MasterLogEntry, lead_uid)
    if lead is None or lead.co_broker == co_broker:
        return None

    already = db.execute(
        select(LeadContributor).where(
            LeadContributor.lead_uid == lead_uid,
            LeadContributor.co_broker == co_broker,
            LeadContributor.reason == reason,
        )
    ).scalar_one_or_none()
    if already is not None:
        return None

    contributor = LeadContributor(lead_uid=lead_uid, co_broker=co_broker, reason=reason)
    db.add(contributor)
    db.commit()
    db.refresh(contributor)
    return contributor


def _attempt_counts(db: Session, campaign_id: uuid.UUID) -> dict[uuid.UUID, tuple[int, DialerDisposition]]:
    """Per-entity (lead_uid or silo_candidate_uid) attempt count and most
    recent disposition within this campaign, used to exclude leads that
    have exhausted max_attempts_per_lead or already been dispositioned."""
    rows = db.execute(
        select(DialerCallAttempt.lead_uid, DialerCallAttempt.silo_candidate_uid, DialerCallAttempt.disposition)
        .where(DialerCallAttempt.campaign_id == campaign_id)
        .order_by(DialerCallAttempt.created_at.asc())
    ).all()
    counts: dict[uuid.UUID, tuple[int, DialerDisposition]] = {}
    for lead_uid, silo_candidate_uid, disposition in rows:
        key = lead_uid or silo_candidate_uid
        if key is None:
            continue
        prior_count, _ = counts.get(key, (0, DialerDisposition.UNSET))
        counts[key] = (prior_count + 1, disposition)
    return counts


def _globally_purged_entities(db: Session) -> set[uuid.UUID]:
    """Real compliance gap this closes: _attempt_counts above only ever
    looks at ONE campaign's own attempt history, so a lead or candidate
    that was PURGEd (a real "don't call back" signal -- see
    DialerDisposition's docstring) in Campaign A could still be dialed by
    Campaign B if it happens to match B's filter too, since B has no idea
    A ever called. A do-not-call signal has to hold across every
    campaign, not just the one that recorded it -- this scans every
    DialerCallAttempt regardless of campaign_id and returns every
    lead_uid/silo_candidate_uid with a PURGE anywhere in its history.
    Deliberately PURGE-only, not every disposition: ADVANCE just means
    "this campaign converted/advanced it," which shouldn't silently
    block an unrelated campaign (e.g. a later Status // Chase Docs
    follow-up) from working that same lead on purpose."""
    purged: set[uuid.UUID] = set()
    rows = db.execute(
        select(DialerCallAttempt.lead_uid, DialerCallAttempt.silo_candidate_uid)
        .where(DialerCallAttempt.disposition == DialerDisposition.PURGE)
    ).all()
    for lead_uid, silo_candidate_uid in rows:
        if lead_uid is not None:
            purged.add(lead_uid)
        if silo_candidate_uid is not None:
            purged.add(silo_candidate_uid)
    return purged


def build_dial_queue(db: Session, campaign: DialerCampaign) -> list[DialQueueEntry]:
    """Resolves campaign.lead_filter into concrete leads to call. All
    configured MasterLogEntry-side filters (calendar / master_log_statuses
    / co_brokers) AND together into one Sheet-lead query -- if two
    programs need two different lead pools, per the request that's meant
    to be two campaigns (each with its own number pool), not one campaign
    with an OR'd filter."""
    filt = campaign.lead_filter or {}
    attempts = _attempt_counts(db, campaign.id)
    do_not_call = _globally_purged_entities(db)
    entries: list[DialQueueEntry] = []

    ml_conditions = []
    if filt.get("calendar"):
        ml_conditions.append(MasterLogEntry.follow_up_date.is_not(None))
    if filt.get("master_log_statuses"):
        ml_conditions.append(MasterLogEntry.status.in_([MasterLogStatus(s) for s in filt["master_log_statuses"]]))
    if filt.get("co_brokers"):
        ml_conditions.append(MasterLogEntry.co_broker.in_(filt["co_brokers"]))

    if ml_conditions:
        # Prioritized, not arbitrary DB order: soonest follow-up due
        # first, then whichever lead has gone longest without a touch --
        # with 5 phones as the hard constraint, the software's job is
        # making sure the next lead worked is the most urgent one on
        # file, not just whichever one the query happened to return
        # first.
        query = (
            select(MasterLogEntry)
            .where(MasterLogEntry.phone.is_not(None), *ml_conditions)
            .order_by(MasterLogEntry.follow_up_date.asc().nulls_last(), MasterLogEntry.updated_at.asc())
        )
        source_label = "calendar" if filt.get("calendar") else "sheet_lead"
        for entry in db.execute(query).scalars().all():
            if entry.lead_uid in do_not_call:
                continue
            count, disposition = attempts.get(entry.lead_uid, (0, DialerDisposition.UNSET))
            if disposition != DialerDisposition.UNSET or count >= campaign.max_attempts_per_lead:
                continue
            entries.append(DialQueueEntry(source_label, entry.lead_uid, entry.business_name, entry.phone, count))

    if filt.get("silos"):
        from app.models.enums import SiloName

        # Prioritized by score -- the real lending-appetite signal the
        # waterfall already computed, not a fabricated ranking. Highest
        # score (strongest signal) worked first.
        conditions = [
            SiloCandidate.silo.in_([SiloName(s) for s in filt["silos"]]),
            SiloCandidate.status == SiloCandidateStatus.PENDING,
            SiloCandidate.phone.is_not(None),
        ]
        # Siphon filters -- carve out a deliberate test slice of a silo
        # instead of dialing the whole thing, so a campaign's own call
        # outcomes (funded / docs-in / dead) can be attributed back to
        # exactly which signal or score band produced them. min_score
        # narrows to the strongest-scored candidates; notes_contains
        # narrows to whichever of silo_leadgen.py's own marker strings
        # (TARIFF-AFFECTED SOURCING, ASIA SOURCING CONFIRMED, etc. -- see
        # _SIGNAL_PERFORMANCE_MARKERS) actually fired on that candidate,
        # matched as an OR, same as every other "any of" filter here.
        # Both are optional and combine with the silo filter above, not
        # with each other's absence -- a campaign can set either, both,
        # or neither.
        if filt.get("min_score") is not None:
            conditions.append(SiloCandidate.score >= filt["min_score"])
        if filt.get("notes_contains"):
            conditions.append(or_(*(SiloCandidate.notes.ilike(f"%{term}%") for term in filt["notes_contains"])))

        query = (
            select(SiloCandidate)
            .where(*conditions)
            .order_by(SiloCandidate.score.desc().nulls_last())
        )
        for candidate in db.execute(query).scalars().all():
            if candidate.candidate_uid in do_not_call:
                continue
            count, disposition = attempts.get(candidate.candidate_uid, (0, DialerDisposition.UNSET))
            if disposition != DialerDisposition.UNSET or count >= campaign.max_attempts_per_lead:
                continue
            entries.append(
                DialQueueEntry("silo_candidate", candidate.candidate_uid, candidate.company_name, candidate.phone, count)
            )

    # Optional deterministic split -- e.g. {"shard": {"of": 2, "index": 0}}
    # lets two callers split the SAME filtered pool (same silo, same
    # min_score/notes_contains) into non-overlapping halves with zero
    # coordination and zero risk of both dialing the same lead the same
    # day. Pure function of each entity's own UUID (last byte mod "of"),
    # not call order or timing -- campaign A's "index": 0 and campaign
    # B's "index": 1 partition the pool identically every time this
    # runs, independent of which one runs first. Deliberately NOT based
    # on attempts/disposition (that's already handled above, per
    # campaign) -- this is about dividing fresh leads between two
    # people, not about who's already been called.
    shard = filt.get("shard")
    if shard:
        of, index = shard["of"], shard["index"]
        entries = [e for e in entries if e.entity_uid.bytes[-1] % of == index]

    cap = min(campaign.max_list_size or HARD_MAX_LIST_SIZE, HARD_MAX_LIST_SIZE)
    return entries[:cap]


def _select_local_number(db: Session, campaign: DialerCampaign, to_number: str) -> DialerNumber | None:
    if campaign.number_pool_id is None:
        return None
    target_area_code = _area_code(to_number)
    query = select(DialerNumber).where(DialerNumber.pool_id == campaign.number_pool_id, DialerNumber.is_active.is_(True))
    candidates = db.execute(query).scalars().all()
    if not candidates:
        return None
    if target_area_code:
        for number in candidates:
            if number.area_code == target_area_code:
                return number
    return candidates[0]


def _within_calling_hours(settings) -> bool:
    """Server-local hour check -- see config.py's dialer_calling_hours_*
    docstring for the honest caveat on what this isn't (the lead's own
    timezone). Wraps correctly for a window that doesn't cross midnight
    (the only shape these two settings are meant to express -- a
    start < end business-hours band)."""
    start, end = settings.dialer_calling_hours_start_hour, settings.dialer_calling_hours_end_hour
    return start <= datetime.now().hour < end


def place_outbound_call(
    db: Session, campaign: DialerCampaign, entry: DialQueueEntry, adapter: SignalWireAdapter | None = None
) -> DialerCallAttempt:
    """Places one outbound call for one dial-queue entry, always
    persisting a DialerCallAttempt row -- even when SignalWire isn't
    configured, no number/phone is available, or the placement itself
    fails -- so every dial attempt (successful or not) shows up in the
    campaign dashboard with an explanatory error rather than silently
    vanishing."""
    adapter = adapter or get_signalwire_adapter()
    settings = get_settings()

    attempt = DialerCallAttempt(
        campaign_id=campaign.id,
        lead_uid=entry.entity_uid if entry.lead_source in ("calendar", "sheet_lead") else None,
        silo_candidate_uid=entry.entity_uid if entry.lead_source == "silo_candidate" else None,
        to_number=entry.to_number or "",
        attempt_number=entry.attempts_so_far + 1,
        status=DialerCallStatus.QUEUED,
    )
    db.add(attempt)
    db.flush()

    if not _within_calling_hours(settings):
        attempt.status = DialerCallStatus.FAILED
        attempt.error = (
            f"outside configured calling-hours window "
            f"({settings.dialer_calling_hours_start_hour:02d}:00-{settings.dialer_calling_hours_end_hour:02d}:00 server-local)"
        )
        db.commit()
        return attempt

    if not entry.to_number:
        attempt.status = DialerCallStatus.FAILED
        attempt.error = "no phone number on file for this lead"
        db.commit()
        return attempt

    if not adapter.enabled:
        attempt.status = DialerCallStatus.FAILED
        attempt.error = "SignalWire is not configured (signalwire_project_id/api_token/space_url)"
        db.commit()
        return attempt

    if not settings.dialer_public_base_url:
        attempt.status = DialerCallStatus.FAILED
        attempt.error = "dialer_public_base_url is not configured -- SignalWire has no callback URL to reach"
        db.commit()
        return attempt

    number = _select_local_number(db, campaign, entry.to_number)
    if number is None:
        attempt.status = DialerCallStatus.FAILED
        attempt.error = "no active number available in this campaign's number pool"
        db.commit()
        return attempt
    attempt.from_number_id = number.id

    base = settings.dialer_public_base_url.rstrip("/")
    laml_url = f"{base}/api/dialer/laml/outbound/{attempt.id}"
    status_url = f"{base}/api/dialer/webhooks/status/{attempt.id}"
    try:
        result = adapter.place_call(
            to_number=entry.to_number, from_number=number.phone_number, laml_url=laml_url, status_callback_url=status_url
        )
        attempt.signalwire_call_sid = result.get("sid")
        raw_status = result.get("status")
        try:
            attempt.status = DialerCallStatus(raw_status)
        except ValueError:
            attempt.status = DialerCallStatus.QUEUED
        attempt.placed_at = datetime.now(timezone.utc)
    except Exception as exc:  # noqa: BLE001 -- one failed placement must not break a batch run
        attempt.status = DialerCallStatus.FAILED
        attempt.error = str(exc)
        logger.exception("SignalWire call placement failed for campaign %s", campaign.id)

    db.commit()
    db.refresh(attempt)
    return attempt


def record_inbound_answer(db: Session, attempt: DialerCallAttempt, co_broker: CoBroker) -> DialerCallAttempt:
    """Called from the per-ring-target statusCallback once a specific leg
    reports answered (see this module's HONESTY NOTE on that mechanism).
    Idempotent against duplicate webhook deliveries -- SignalWire, like
    Twilio, does not guarantee exactly-once webhook delivery -- since
    setting the same status/broker twice is harmless and
    credit_contributor already no-ops a repeat credit."""
    attempt.status = DialerCallStatus.IN_PROGRESS
    attempt.answered_by_co_broker = co_broker
    db.commit()

    if attempt.lead_uid is not None:
        credit_contributor(db, attempt.lead_uid, co_broker, LeadContributionReason.INBOUND_CALL_ANSWERED)

    db.refresh(attempt)
    return attempt


def run_campaign(db: Session, campaign: DialerCampaign, max_calls: int = 10) -> list[DialerCallAttempt]:
    """Builds the dial queue and places up to `max_calls` outbound calls.
    `max_calls` is a manual per-sweep batch cap -- not the requested
    predictive volume-pacing optimizer, which needs real answer-rate data
    this system doesn't have yet and is deferred to a follow-up phase."""
    if not campaign.is_active:
        raise ValueError("campaign is not active")
    queue = build_dial_queue(db, campaign)
    adapter = get_signalwire_adapter()
    return [place_outbound_call(db, campaign, entry, adapter) for entry in queue[:max_calls]]


def process_call_status(
    db: Session,
    attempt: DialerCallAttempt,
    *,
    call_status: str,
    duration_seconds: float | None = None,
    recording_url: str | None = None,
) -> DialerCallAttempt:
    try:
        attempt.status = DialerCallStatus(call_status)
    except ValueError:
        logger.warning("Unrecognized SignalWire call status %r for attempt %s", call_status, attempt.id)
    if duration_seconds is not None:
        attempt.duration_seconds = duration_seconds
    is_new_recording = bool(recording_url) and recording_url != attempt.recording_url
    if recording_url:
        attempt.recording_url = recording_url
    db.commit()
    db.refresh(attempt)

    if is_new_recording:
        auto_analyze_recording(db, attempt)
    return attempt


def auto_analyze_recording(db: Session, attempt: DialerCallAttempt) -> None:
    """Every recorded call -- outbound campaign, inbound, or voicemail
    fallback -- gets transcribed/diarized/sentiment-scored automatically
    the moment its recording lands, instead of waiting for someone to
    paste the URL into the Intelligence page by hand. This is the actual
    mechanism behind "the Brain observes every call": the resulting
    CallRecording feeds back into services/brain.py's feature set (see
    _lead_features there), so call behavior -- not just deal metadata --
    shapes the funded-probability score. No-ops quietly if Deepgram isn't
    configured (call_analysis.analyze_call's existing no-key-no-op
    pattern), so a call still gets logged even where transcription can't
    run."""
    try:
        call_analysis.analyze_call(
            db,
            source_label=f"dialer_attempt:{attempt.id}",
            lead_uid=attempt.lead_uid,
            audio_url=attempt.recording_url,
        )
    except Exception:  # noqa: BLE001 -- a failed auto-analysis must not break call-status processing
        logger.exception("Auto call-analysis failed for attempt %s", attempt.id)


def apply_disposition(
    db: Session,
    attempt: DialerCallAttempt,
    disposition: DialerDisposition,
    *,
    co_broker=None,
    advance_status: MasterLogStatus = MasterLogStatus.IN_NEGOTIATION,
    purge_status: MasterLogStatus = MasterLogStatus.GHOSTED,
    source: ActivitySource = ActivitySource.API,
) -> DialerCallAttempt:
    """The human call-outcome decision -- routes through the same pipeline
    mutations the manual silo Convert/Dismiss buttons use. A silo
    candidate gets freshly converted/dismissed; a lead already in Master
    Log V2 (a calendar/sheet lead) advances to `advance_status` or
    `purge_status` instead, since it has no "convert" step left to take."""
    if attempt.disposition != DialerDisposition.UNSET:
        raise ValueError("this call attempt already has a disposition set")
    if disposition == DialerDisposition.UNSET:
        raise ValueError("disposition must be advance or purge")

    if attempt.silo_candidate_uid is not None:
        candidate = db.get(SiloCandidate, attempt.silo_candidate_uid)
        if candidate is None:
            raise ValueError("silo candidate no longer exists")
        target_status = SiloCandidateStatus.CONVERTED if disposition == DialerDisposition.ADVANCE else SiloCandidateStatus.DISMISSED
        pipeline.convert_or_update_silo_candidate(db, candidate, target_status, co_broker, source=source)
    elif attempt.lead_uid is not None:
        entry = db.get(MasterLogEntry, attempt.lead_uid)
        if entry is None:
            raise ValueError("lead no longer exists")
        target_status = advance_status if disposition == DialerDisposition.ADVANCE else purge_status
        pipeline.update_lead_status(db, entry, target_status, source=source)
    else:
        raise ValueError("call attempt has no linked lead or silo candidate to disposition")

    attempt.disposition = disposition
    db.commit()
    db.refresh(attempt)
    return attempt


# ---------------------------------------------------------------------------
# Bounded Pacing Scheduler -- meters every active campaign's dial queue in
# small, steady batches across a shift instead of one big run_campaign
# call. Off by default; same background-loop shape as brain.py's
# _refresh_loop / sheets_sync.py's _pull_loop, deliberately copied rather
# than reinvented.
# ---------------------------------------------------------------------------

_pacing_task: asyncio.Task | None = None


def _pace_all_active_campaigns(db: Session) -> dict[str, int]:
    """One tick: run_campaign(max_calls=dialer_pacing_batch_size) for
    every is_active campaign, via place_outbound_call -- which already
    does its own calling-hours check per call, so a tick that starts
    just before the window closes still can't place a late call, and a
    tick during closed hours places zero calls for every campaign rather
    than needing a separate skip here. _select_local_number (inside
    place_outbound_call) is what actually picks the caller-ID number per
    call -- this loop only decides WHEN and HOW MANY, never which
    number."""
    settings = get_settings()
    placed: dict[str, int] = {}
    campaigns = db.execute(select(DialerCampaign).where(DialerCampaign.is_active.is_(True))).scalars().all()
    for campaign in campaigns:
        attempts = run_campaign(db, campaign, max_calls=settings.dialer_pacing_batch_size)
        if attempts:
            placed[campaign.name] = len(attempts)
    return placed


async def _pacing_loop() -> None:
    from app.database import SessionLocal

    while True:
        try:
            db = SessionLocal()
            try:
                placed = await asyncio.to_thread(_pace_all_active_campaigns, db)
                if placed:
                    logger.info("Dialer pacing tick placed calls: %s", placed)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 -- one bad tick must not kill the loop
            logger.exception("Dialer pacing tick failed -- will retry next interval")
        await asyncio.sleep(get_settings().dialer_pacing_interval_seconds)


def start_pacing_loop() -> asyncio.Task | None:
    """Off unless dialer_pacing_enabled=true -- this places real outbound
    calls on its own schedule, same explicit-opt-in posture as river
    surveillance and the calendar/sheet pulls, not pure local
    computation like brain's refresh loop."""
    global _pacing_task
    if not get_settings().dialer_pacing_enabled:
        logger.info("Dialer pacing disabled (set DIALER_PACING_ENABLED=true to enable)")
        return None
    if _pacing_task is None or _pacing_task.done():
        _pacing_task = asyncio.create_task(_pacing_loop())
        logger.info("Dialer pacing loop started (interval=%ss, batch=%s)", get_settings().dialer_pacing_interval_seconds, get_settings().dialer_pacing_batch_size)
    return _pacing_task


def stop_pacing_loop() -> None:
    global _pacing_task
    if _pacing_task is not None:
        _pacing_task.cancel()
        _pacing_task = None
        logger.info("Dialer pacing loop stopped")
