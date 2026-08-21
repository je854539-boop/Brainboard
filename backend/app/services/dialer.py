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

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
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
)
from app.models.orm import (
    DialerCallAttempt,
    DialerCampaign,
    DialerNumber,
    InboundRingTarget,
    LeadContributor,
    MasterLogEntry,
    SiloCandidate,
)
from app.services import pipeline
from app.services.signalwire_adapter import SignalWireAdapter, get_signalwire_adapter

logger = logging.getLogger("brainboard.dialer")


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


def build_dial_queue(db: Session, campaign: DialerCampaign) -> list[DialQueueEntry]:
    """Resolves campaign.lead_filter into concrete leads to call. All
    configured MasterLogEntry-side filters (calendar / master_log_statuses
    / co_brokers) AND together into one Sheet-lead query -- if two
    programs need two different lead pools, per the request that's meant
    to be two campaigns (each with its own number pool), not one campaign
    with an OR'd filter."""
    filt = campaign.lead_filter or {}
    attempts = _attempt_counts(db, campaign.id)
    entries: list[DialQueueEntry] = []

    ml_conditions = []
    if filt.get("calendar"):
        ml_conditions.append(MasterLogEntry.follow_up_date.is_not(None))
    if filt.get("master_log_statuses"):
        ml_conditions.append(MasterLogEntry.status.in_([MasterLogStatus(s) for s in filt["master_log_statuses"]]))
    if filt.get("co_brokers"):
        ml_conditions.append(MasterLogEntry.co_broker.in_(filt["co_brokers"]))

    if ml_conditions:
        query = select(MasterLogEntry).where(MasterLogEntry.phone.is_not(None), *ml_conditions)
        source_label = "calendar" if filt.get("calendar") else "sheet_lead"
        for entry in db.execute(query).scalars().all():
            count, disposition = attempts.get(entry.lead_uid, (0, DialerDisposition.UNSET))
            if disposition != DialerDisposition.UNSET or count >= campaign.max_attempts_per_lead:
                continue
            entries.append(DialQueueEntry(source_label, entry.lead_uid, entry.business_name, entry.phone, count))

    if filt.get("silos"):
        from app.models.enums import SiloName

        query = select(SiloCandidate).where(
            SiloCandidate.silo.in_([SiloName(s) for s in filt["silos"]]),
            SiloCandidate.status == SiloCandidateStatus.PENDING,
            SiloCandidate.phone.is_not(None),
        )
        for candidate in db.execute(query).scalars().all():
            count, disposition = attempts.get(candidate.candidate_uid, (0, DialerDisposition.UNSET))
            if disposition != DialerDisposition.UNSET or count >= campaign.max_attempts_per_lead:
                continue
            entries.append(
                DialQueueEntry("silo_candidate", candidate.candidate_uid, candidate.company_name, candidate.phone, count)
            )

    return entries


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
    if recording_url:
        attempt.recording_url = recording_url
    db.commit()
    db.refresh(attempt)
    return attempt


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
