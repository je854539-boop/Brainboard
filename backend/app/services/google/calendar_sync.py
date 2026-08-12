"""Calendar sync triggered by Master Log V2 Column K (follow-up date) /
Column L (notes) edits, keyed on `extendedProperties.private.lead_uid`.

`onMasterLogEdit` in the Apps Script (see apps_script/Code.gs) calls the
`/webhooks/master-log-edit` FastAPI route on a Column K/L mutation, which
calls `upsert_follow_up_event` below to create/update the matching
Calendar event so the operator's calendar always reflects the current
follow-up date + dossier notes for that lead.
"""

import datetime as dt

from app.config import get_settings
from app.models.orm import MasterLogEntry


def _find_event_by_lead_uid(service, calendar_id: str, lead_uid: str) -> dict | None:
    response = (
        service.events()
        .list(calendarId=calendar_id, privateExtendedProperty=f"lead_uid={lead_uid}", maxResults=1)
        .execute()
    )
    items = response.get("items", [])
    return items[0] if items else None


def upsert_follow_up_event(service, lead: MasterLogEntry) -> str | None:
    """Create/update the Calendar event for this lead's follow-up date.
    Returns the Calendar event ID, or None if there's no follow-up date to
    schedule (existing event is deleted in that case)."""
    settings = get_settings()
    calendar_id = settings.google_calendar_id
    lead_uid = str(lead.lead_uid)

    existing = _find_event_by_lead_uid(service, calendar_id, lead_uid)

    if lead.follow_up_date is None:
        if existing:
            service.events().delete(calendarId=calendar_id, eventId=existing["id"]).execute()
        return None

    start = lead.follow_up_date
    end = start + dt.timedelta(minutes=30)

    body = {
        "summary": f"Follow-up: {lead.business_name} ({lead.status.value})",
        "description": (
            f"Lead UID: {lead_uid}\n"
            f"Co-broker: {lead.co_broker.value}\n"
            f"Status: {lead.status.value}\n"
            f"Macro dossier: {lead.dossier_drive_link or '(none)'}\n\n"
            f"Notes:\n{lead.notes or ''}"
        ),
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "extendedProperties": {"private": {"lead_uid": lead_uid}},
    }

    if existing:
        event = service.events().patch(calendarId=calendar_id, eventId=existing["id"], body=body).execute()
    else:
        event = service.events().insert(calendarId=calendar_id, body=body).execute()

    return event["id"]
