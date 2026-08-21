"""SignalWire dialer: campaign/number-pool management API plus the two
endpoints SignalWire itself calls back into -- the LaML call-control
document fetched once an outbound call connects, and the call-status
webhook. Those two are NOT behind the app's own X-Webhook-Secret header
(SignalWire can't send it) -- they're instead validated with SignalWire's
own webhook-signature scheme via signalwire_adapter.verify_webhook_signature
when SIGNALWIRE_WEBHOOK_SIGNING_KEY is configured (best-effort/skipped
otherwise, see that function's docstring for what's confirmed vs assumed).
"""

import uuid
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.enums import ActivitySource, DialerCallDirection, DialerCallStatus, DialerDisposition
from app.models.orm import (
    DialerCallAttempt,
    DialerCampaign,
    DialerNumber,
    DialerNumberPool,
    EnrichmentResult,
    InboundRingTarget,
    LeadActivityEvent,
    MasterLogEntry,
    SiloCandidate,
)
from app.schemas import (
    DialerCallAttemptOut,
    DialerCallNowRequest,
    DialerCampaignCreate,
    DialerCampaignOut,
    DialerCampaignUpdate,
    DialerDispositionUpdate,
    DialerNumberCreate,
    DialerNumberOut,
    DialerNumberPoolCreate,
    DialerNumberPoolOut,
    DialerQueuePreviewEntry,
    InboundRingTargetCreate,
    InboundRingTargetOut,
)
from app.services import dialer
from app.services.signalwire_adapter import get_signalwire_adapter

router = APIRouter(prefix="/api/dialer", tags=["dialer"])


@router.get("/status")
def dialer_status():
    """SignalWire configuration status for the campaign dashboard's
    provider-status panel -- never leaks the actual token."""
    settings = get_settings()
    adapter = get_signalwire_adapter()
    return {
        "signalwire_enabled": adapter.enabled,
        "space_url": settings.signalwire_space_url or None,
        "callback_base_url_configured": bool(settings.dialer_public_base_url),
        "webhook_signing_configured": bool(settings.signalwire_webhook_signing_key),
    }


# ---- number pools / numbers ------------------------------------------------


@router.post("/pools", response_model=DialerNumberPoolOut, status_code=201)
def create_pool(payload: DialerNumberPoolCreate, db: Session = Depends(get_db)):
    pool = DialerNumberPool(**payload.model_dump())
    db.add(pool)
    db.commit()
    db.refresh(pool)
    return pool


@router.get("/pools", response_model=list[DialerNumberPoolOut])
def list_pools(db: Session = Depends(get_db)):
    return db.execute(select(DialerNumberPool).order_by(DialerNumberPool.created_at.desc())).scalars().all()


@router.post("/numbers", response_model=DialerNumberOut, status_code=201)
def add_number(payload: DialerNumberCreate, db: Session = Depends(get_db)):
    if db.get(DialerNumberPool, payload.pool_id) is None:
        raise HTTPException(status_code=404, detail="number pool not found")
    number = DialerNumber(**payload.model_dump())
    db.add(number)
    db.commit()
    db.refresh(number)
    return number


# ---- inbound ring group -----------------------------------------------------


@router.post("/inbound-roster", response_model=InboundRingTargetOut, status_code=201)
def add_ring_target(payload: InboundRingTargetCreate, db: Session = Depends(get_db)):
    target = InboundRingTarget(**payload.model_dump())
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


@router.get("/inbound-roster", response_model=list[InboundRingTargetOut])
def list_ring_targets(db: Session = Depends(get_db)):
    return db.execute(select(InboundRingTarget).order_by(InboundRingTarget.created_at)).scalars().all()


@router.patch("/inbound-roster/{target_id}", response_model=InboundRingTargetOut)
def update_ring_target(target_id: uuid.UUID, is_active: bool, db: Session = Depends(get_db)):
    target = db.get(InboundRingTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="ring target not found")
    target.is_active = is_active
    db.commit()
    db.refresh(target)
    return target


# ---- campaigns --------------------------------------------------------------


@router.post("/campaigns", response_model=DialerCampaignOut, status_code=201)
def create_campaign(payload: DialerCampaignCreate, db: Session = Depends(get_db)):
    campaign = DialerCampaign(**payload.model_dump())
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.get("/campaigns", response_model=list[DialerCampaignOut])
def list_campaigns(db: Session = Depends(get_db)):
    return db.execute(select(DialerCampaign).order_by(DialerCampaign.created_at.desc())).scalars().all()


@router.get("/campaigns/{campaign_id}", response_model=DialerCampaignOut)
def get_campaign(campaign_id: uuid.UUID, db: Session = Depends(get_db)):
    campaign = db.get(DialerCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return campaign


@router.patch("/campaigns/{campaign_id}", response_model=DialerCampaignOut)
def update_campaign(campaign_id: uuid.UUID, payload: DialerCampaignUpdate, db: Session = Depends(get_db)):
    campaign = db.get(DialerCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(campaign, field, value)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.get("/campaigns/{campaign_id}/queue-preview", response_model=list[DialerQueuePreviewEntry])
def preview_queue(campaign_id: uuid.UUID, db: Session = Depends(get_db)):
    campaign = db.get(DialerCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return [
        DialerQueuePreviewEntry(
            lead_source=e.lead_source,
            entity_uid=e.entity_uid,
            company_name=e.company_name,
            to_number=e.to_number,
            attempts_so_far=e.attempts_so_far,
        )
        for e in dialer.build_dial_queue(db, campaign)
    ]


@router.post("/campaigns/{campaign_id}/run", response_model=list[DialerCallAttemptOut])
def run_campaign(campaign_id: uuid.UUID, max_calls: int = 10, db: Session = Depends(get_db)):
    campaign = db.get(DialerCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    try:
        return dialer.run_campaign(db, campaign, max_calls=min(max_calls, 100))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/campaigns/{campaign_id}/call-now", response_model=DialerCallAttemptOut)
def call_now(campaign_id: uuid.UUID, payload: DialerCallNowRequest, db: Session = Depends(get_db)):
    """Click-to-call: places one ad hoc call right now, outside any queue
    sweep, reusing campaign_id's number pool / caller_connect_number /
    pitch_recording_url as-is -- this is what the "active line" selector
    in the header points at. Ignores max_attempts_per_lead, since a
    deliberate manual dial isn't something the pacing cap should block."""
    campaign = db.get(DialerCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    if bool(payload.lead_uid) == bool(payload.silo_candidate_uid):
        raise HTTPException(status_code=422, detail="exactly one of lead_uid or silo_candidate_uid is required")

    if payload.lead_uid:
        lead = db.get(MasterLogEntry, payload.lead_uid)
        if lead is None:
            raise HTTPException(status_code=404, detail="lead not found")
        if not lead.phone:
            raise HTTPException(status_code=422, detail="lead has no phone number on file")
        source, entity_uid, company_name, to_number = "sheet_lead", lead.lead_uid, lead.business_name, lead.phone
        count_filter = DialerCallAttempt.lead_uid == entity_uid
    else:
        candidate = db.get(SiloCandidate, payload.silo_candidate_uid)
        if candidate is None:
            raise HTTPException(status_code=404, detail="silo candidate not found")
        if not candidate.phone:
            raise HTTPException(status_code=422, detail="candidate has no phone number on file")
        source, entity_uid, company_name, to_number = (
            "silo_candidate", candidate.candidate_uid, candidate.company_name, candidate.phone,
        )
        count_filter = DialerCallAttempt.silo_candidate_uid == entity_uid

    prior_attempts = db.execute(select(func.count()).select_from(DialerCallAttempt).where(count_filter)).scalar_one()
    entry = dialer.DialQueueEntry(source, entity_uid, company_name, to_number, prior_attempts)
    return dialer.place_outbound_call(db, campaign, entry)


@router.get("/attempts/active")
def active_attempts(db: Session = Depends(get_db)):
    """Polled by the header's screen-pop widget every few seconds --
    returns call attempts from the last 10 minutes still in a live state
    (queued/ringing/in-progress), so the widget can catch one flipping to
    in-progress and trigger the notes/enrichment pop for that lead."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    query = (
        select(DialerCallAttempt)
        .where(
            DialerCallAttempt.created_at >= cutoff,
            DialerCallAttempt.status.in_(
                [DialerCallStatus.QUEUED, DialerCallStatus.RINGING, DialerCallStatus.IN_PROGRESS]
            ),
        )
        .order_by(DialerCallAttempt.created_at.desc())
    )
    attempts = db.execute(query).scalars().all()
    return [
        {
            "id": str(a.id),
            "status": a.status.value,
            "direction": a.direction.value,
            "to_number": a.to_number,
            "lead_uid": str(a.lead_uid) if a.lead_uid else None,
            "silo_candidate_uid": str(a.silo_candidate_uid) if a.silo_candidate_uid else None,
            "created_at": a.created_at.isoformat(),
        }
        for a in attempts
    ]


@router.get("/screen-pop/{entity_uid}")
def screen_pop(entity_uid: uuid.UUID, db: Session = Depends(get_db)):
    """Notes + recent activity + enrichment for one lead or silo candidate
    in a single call -- what the floating screen-pop panel renders once
    the active-attempts poll catches a call going live."""
    lead = db.get(MasterLogEntry, entity_uid)
    if lead is not None:
        activity = db.execute(
            select(LeadActivityEvent)
            .where(LeadActivityEvent.lead_uid == entity_uid)
            .order_by(LeadActivityEvent.occurred_at.desc())
            .limit(15)
        ).scalars().all()
        enrichment = db.execute(
            select(EnrichmentResult).where(EnrichmentResult.entity_uid == entity_uid).order_by(EnrichmentResult.created_at.desc())
        ).scalars().all()
        return {
            "kind": "lead",
            "entity_uid": str(entity_uid),
            "company_name": lead.business_name,
            "contact_name": lead.contact_name,
            "phone": lead.phone,
            "email": lead.email,
            "status": lead.status.value,
            "notes": lead.notes,
            "dossier_drive_link": lead.dossier_drive_link,
            "financials_link": lead.financials_link,
            "transcripts_link": lead.transcripts_link,
            "activity": [
                {
                    "event_type": a.event_type.value,
                    "field_name": a.field_name,
                    "old_value": a.old_value,
                    "new_value": a.new_value,
                    "occurred_at": a.occurred_at.isoformat(),
                }
                for a in activity
            ],
            "enrichment": [
                {"source": r.source.value, "payload": r.payload, "confidence": float(r.confidence) if r.confidence is not None else None}
                for r in enrichment
            ],
        }

    candidate = db.get(SiloCandidate, entity_uid)
    if candidate is not None:
        enrichment = db.execute(
            select(EnrichmentResult).where(EnrichmentResult.entity_uid == entity_uid).order_by(EnrichmentResult.created_at.desc())
        ).scalars().all()
        return {
            "kind": "silo_candidate",
            "entity_uid": str(entity_uid),
            "company_name": candidate.company_name,
            "contact_name": candidate.contact_name,
            "phone": candidate.phone,
            "email": candidate.email,
            "silo": candidate.silo.value,
            "status": candidate.status.value,
            "notes": candidate.notes,
            "score": float(candidate.score) if candidate.score is not None else None,
            "activity": [],
            "enrichment": [
                {"source": r.source.value, "payload": r.payload, "confidence": float(r.confidence) if r.confidence is not None else None}
                for r in enrichment
            ],
        }

    raise HTTPException(status_code=404, detail="entity not found")


@router.get("/attempts/inbound", response_model=list[DialerCallAttemptOut])
def list_inbound_attempts(limit: int = 30, db: Session = Depends(get_db)):
    """Inbound calls have no campaign_id (they're not placed by any
    outbound campaign), so they'd never show up in the per-campaign
    attempts table -- this is their own view, most recent first."""
    query = (
        select(DialerCallAttempt)
        .where(DialerCallAttempt.direction == DialerCallDirection.INBOUND)
        .order_by(DialerCallAttempt.created_at.desc())
        .limit(min(limit, 200))
    )
    return db.execute(query).scalars().all()


@router.get("/campaigns/{campaign_id}/attempts", response_model=list[DialerCallAttemptOut])
def list_attempts(campaign_id: uuid.UUID, db: Session = Depends(get_db)):
    query = (
        select(DialerCallAttempt)
        .where(DialerCallAttempt.campaign_id == campaign_id)
        .order_by(DialerCallAttempt.created_at.desc())
    )
    return db.execute(query).scalars().all()


@router.post("/attempts/{attempt_id}/disposition", response_model=DialerCallAttemptOut)
def disposition_attempt(attempt_id: uuid.UUID, payload: DialerDispositionUpdate, db: Session = Depends(get_db)):
    attempt = db.get(DialerCallAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="call attempt not found")
    try:
        return dialer.apply_disposition(
            db, attempt, payload.disposition, co_broker=payload.co_broker, source=ActivitySource.UI
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---- SignalWire callbacks (not behind X-Webhook-Secret -- see module docstring) --


def _laml(xml_body: str) -> Response:
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}', media_type="application/xml")


@router.api_route("/laml/outbound/{attempt_id}", methods=["GET", "POST"])
async def outbound_laml(attempt_id: uuid.UUID, db: Session = Depends(get_db)):
    """Fetched by SignalWire once the outbound call connects.

    With campaign.pitch_recording_url set: plays that recording (a real
    recording of the broker pitching -- deliberately not synthetic/AI
    voice, see the compliance discussion this was built from) as the
    opener, then always bridges to caller_connect_number -- no keypress
    gate. If the lead hangs up during/after the recording, the call just
    ends there (SignalWire never reaches the <Dial>); if they stay on the
    line, they're connected live. This is a passive filter (self-select
    out by hanging up) rather than an active one (press 1) -- lower
    friction, same effect, and total call duration (captured either way
    via the status webhook below) is still a usable engagement signal
    even without a keypress.

    Without a pitch_recording_url: legacy/simple mode, bridges immediately
    on answer -- NOT the deferred live patch-in/double-dial conferencing,
    which needs a second already-in-progress call to bridge into and isn't
    built yet."""
    attempt = db.get(DialerCallAttempt, attempt_id)
    if attempt is None:
        return _laml("<Response><Say>Call record not found.</Say></Response>")

    campaign = db.get(DialerCampaign, attempt.campaign_id)
    if campaign is None or not campaign.caller_connect_number:
        return _laml(
            "<Response><Say>Thanks for picking up. No follow-up line is configured for this campaign yet.</Say></Response>"
        )

    settings = get_settings()
    base = settings.dialer_public_base_url.rstrip("/")
    status_url = escape(f"{base}/api/dialer/webhooks/status/{attempt.id}")
    number = escape(campaign.caller_connect_number)
    bridge = (
        f'<Dial record="record-from-answer" recordingStatusCallback="{status_url}" '
        f'action="{status_url}"><Number>{number}</Number></Dial>'
    )

    opener = f"<Play>{escape(campaign.pitch_recording_url)}</Play>" if campaign.pitch_recording_url else ""
    return _laml(f"<Response>{opener}{bridge}</Response>")


@router.post("/webhooks/status/{attempt_id}")
async def call_status_webhook(attempt_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    attempt = db.get(DialerCallAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="call attempt not found")

    form = await request.form()
    settings = get_settings()
    adapter = get_signalwire_adapter()
    signature = request.headers.get("X-SignalWire-Signature") or request.headers.get("X-Twilio-Signature") or ""
    if not adapter.verify_webhook_signature(settings.signalwire_webhook_signing_key, str(request.url), dict(form), signature):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    call_status = form.get("CallStatus") or form.get("DialCallStatus")
    if not call_status:
        return {"ok": True, "note": "no CallStatus/DialCallStatus field, ignored"}

    duration_raw = form.get("CallDuration") or form.get("DialCallDuration")
    duration = float(duration_raw) if duration_raw and duration_raw.isdigit() else None
    recording_url = form.get("RecordingUrl")

    dialer.process_call_status(db, attempt, call_status=call_status, duration_seconds=duration, recording_url=recording_url)
    return {"ok": True}


@router.api_route("/webhooks/inbound", methods=["GET", "POST"])
async def inbound_call(request: Request, db: Session = Depends(get_db)):
    """Fetched by SignalWire the instant a merchant calls one of your
    numbers back -- before anyone has picked up. Identifies the caller
    against known leads/silo candidates immediately (see
    dialer.identify_inbound_caller), logs a DialerCallAttempt in
    'ringing' status right away so the screen-pop widget can surface who's
    calling while the phone is still ringing (not just once someone
    answers, unlike the outbound flow -- see this module's dialer.py
    import docstring for why that's possible here), then rings every
    active roster phone simultaneously. First pickup wins; the rest stop
    ringing automatically -- standard multi-<Number> <Dial> behavior. No
    one answers within the timeout -> falls through to voicemail.

    Not signature-checked, matching outbound_laml -- this endpoint only
    reads/creates a call-attempt record, it doesn't mutate an existing
    lead or disposition, so it follows the same "LaML fetch" trust level
    as outbound_laml rather than the mutating webhooks below."""
    form = await request.form()
    from_number = form.get("From") or ""
    settings = get_settings()

    kind, entity_uid, _company_name = dialer.identify_inbound_caller(db, from_number)
    attempt = DialerCallAttempt(
        direction=DialerCallDirection.INBOUND,
        to_number=from_number,
        attempt_number=1,
        status=DialerCallStatus.RINGING,
        lead_uid=entity_uid if kind == "lead" else None,
        silo_candidate_uid=entity_uid if kind == "silo_candidate" else None,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    targets = dialer.active_ring_targets(db)
    if not targets:
        return _laml(
            "<Response><Say>Thanks for calling. No one is available to take your call right now, "
            "please try again shortly.</Say></Response>"
        )

    if not settings.dialer_public_base_url:
        return _laml("<Response><Say>This line is not fully configured yet.</Say></Response>")

    base = settings.dialer_public_base_url.rstrip("/")
    outer_status_url = escape(f"{base}/api/dialer/webhooks/status/{attempt.id}")
    numbers_xml = "".join(
        f'<Number statusCallback="{escape(f"{base}/api/dialer/webhooks/inbound-leg-answered/{attempt.id}?ring_target_id={t.id}")}" '
        f'statusCallbackEvent="answered">{escape(t.phone_number)}</Number>'
        for t in targets
    )
    dial = (
        f'<Dial timeout="25" record="record-from-answer" recordingStatusCallback="{outer_status_url}" '
        f'action="{outer_status_url}">{numbers_xml}</Dial>'
    )
    voicemail_status_url = escape(f"{base}/api/dialer/webhooks/inbound-voicemail/{attempt.id}")
    voicemail = (
        '<Say>Sorry we missed you. Please leave a message after the tone.</Say>'
        f'<Record maxLength="120" recordingStatusCallback="{voicemail_status_url}"/>'
    )
    return _laml(f"<Response>{dial}{voicemail}</Response>")


@router.post("/webhooks/inbound-leg-answered/{attempt_id}")
async def inbound_leg_answered(attempt_id: uuid.UUID, ring_target_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    """Per-<Number> statusCallback from the ring group (see this module's
    dialer.py import docstring's HONESTY NOTE on this mechanism) -- fires
    when THIS specific roster phone answers, which is how we know who won
    the race without any polling or guessing."""
    attempt = db.get(DialerCallAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="call attempt not found")
    target = db.get(InboundRingTarget, ring_target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="ring target not found")

    form = await request.form()
    settings = get_settings()
    adapter = get_signalwire_adapter()
    signature = request.headers.get("X-SignalWire-Signature") or request.headers.get("X-Twilio-Signature") or ""
    if not adapter.verify_webhook_signature(settings.signalwire_webhook_signing_key, str(request.url), dict(form), signature):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    call_status = form.get("CallStatus") or ""
    if call_status not in ("answered", "in-progress"):
        return {"ok": True, "note": f"leg status {call_status!r} is not an answer, ignored"}

    dialer.record_inbound_answer(db, attempt, target.co_broker)
    return {"ok": True}


@router.post("/webhooks/inbound-voicemail/{attempt_id}")
async def inbound_voicemail(attempt_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    """Fired when the fallback <Record> verb finishes -- nobody in the
    ring group answered within the timeout. Distinct from
    /webhooks/status because <Record>'s callback doesn't carry a
    CallStatus/DialCallStatus field the way a Dial/Call callback does, so
    reusing that endpoint would silently no-op instead of saving the
    voicemail."""
    attempt = db.get(DialerCallAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="call attempt not found")

    form = await request.form()
    settings = get_settings()
    adapter = get_signalwire_adapter()
    signature = request.headers.get("X-SignalWire-Signature") or request.headers.get("X-Twilio-Signature") or ""
    if not adapter.verify_webhook_signature(settings.signalwire_webhook_signing_key, str(request.url), dict(form), signature):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    if attempt.status != DialerCallStatus.IN_PROGRESS:  # a leg may have answered a beat after Record started
        attempt.status = DialerCallStatus.NO_ANSWER
    attempt.recording_url = form.get("RecordingUrl")
    duration_raw = form.get("RecordingDuration")
    attempt.duration_seconds = float(duration_raw) if duration_raw and duration_raw.isdigit() else None
    db.commit()
    return {"ok": True}
