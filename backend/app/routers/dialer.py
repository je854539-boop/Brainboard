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
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.enums import ActivitySource, DialerDisposition
from app.models.orm import DialerCallAttempt, DialerCampaign, DialerNumber, DialerNumberPool
from app.schemas import (
    DialerCallAttemptOut,
    DialerCampaignCreate,
    DialerCampaignOut,
    DialerCampaignUpdate,
    DialerDispositionUpdate,
    DialerNumberCreate,
    DialerNumberOut,
    DialerNumberPoolCreate,
    DialerNumberPoolOut,
    DialerQueuePreviewEntry,
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
    """Fetched by SignalWire once the outbound call connects. Bridges to
    the campaign's caller_connect_number when configured (the simple
    "ring me when they pick up" flow) -- NOT the deferred live patch-in/
    double-dial conferencing, which needs a second already-in-progress
    call to bridge into and isn't built yet."""
    attempt = db.get(DialerCallAttempt, attempt_id)
    if attempt is None:
        return _laml("<Response><Say>Call record not found.</Say></Response>")

    campaign = db.get(DialerCampaign, attempt.campaign_id)
    settings = get_settings()
    status_url = f"{settings.dialer_public_base_url.rstrip('/')}/api/dialer/webhooks/status/{attempt.id}"

    if campaign and campaign.caller_connect_number:
        number = escape(campaign.caller_connect_number)
        action = escape(status_url)
        return _laml(
            f'<Response><Dial record="record-from-answer" recordingStatusCallback="{action}" '
            f'action="{action}"><Number>{number}</Number></Dial></Response>'
        )
    return _laml(
        "<Response><Say>Thanks for picking up. No follow-up line is configured for this campaign yet.</Say></Response>"
    )


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
