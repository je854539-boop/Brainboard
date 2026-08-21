"""Shared pipeline mutations used by both the JSON API routers, the
dashboard HTMX endpoints, and the Apps Script webhook handlers, so lead
creation / field updates / status transitions / silo conversion / activity
logging / Calendar sync only happen in one place -- regardless of whether
the change originated in the UI, the JSON API, or an inbound Sheet edit.

Google Sheets is the source of truth: pulling a Sheet row always
overwrites the matching Postgres fields (see
google/sheets_sync.py::pull_master_log), and Calendar always mirrors
Postgres's current follow_up_date/notes, so both directions converge on
whatever the Sheet last said. `update_lead_fields` guards against a sync
loop by only pushing Postgres -> Sheet for changes that did NOT originate
from a Sheet-edit webhook in the first place.
"""

import logging

from sqlalchemy.orm import Session

from app.models.enums import ActivityEventType, ActivitySource, CoBroker, MasterLogStatus, SiloCandidateStatus
from app.models.orm import LeadActivityEvent, MasterLogEntry, SiloCandidate, StatusHistory

logger = logging.getLogger("brainboard.pipeline")

# Fields on MasterLogEntry that get a dedicated ActivityEventType instead
# of the generic FIELD_CHANGE bucket.
_EVENT_TYPE_BY_FIELD = {
    "notes": ActivityEventType.NOTE_CHANGE,
    "follow_up_date": ActivityEventType.FOLLOW_UP_CHANGE,
    "status": ActivityEventType.STATUS_CHANGE,
}

# Changing either of these on a lead must re-sync the linked Calendar event.
# Status changes are handled separately below (see update_lead_fields) --
# "status" is popped out of `updates` before this set is checked, since it
# routes through update_lead_status instead of the generic field loop.
_CALENDAR_TRIGGER_FIELDS = {"notes", "follow_up_date"}


def log_activity(
    db: Session,
    lead_uid,
    event_type: ActivityEventType,
    source: ActivitySource,
    *,
    field_name: str | None = None,
    old_value=None,
    new_value=None,
) -> LeadActivityEvent:
    event = LeadActivityEvent(
        lead_uid=lead_uid,
        event_type=event_type,
        source=source,
        field_name=field_name,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None,
    )
    db.add(event)
    return event


def sync_calendar(db: Session, entry: MasterLogEntry, source: ActivitySource) -> None:
    """Best-effort push of entry.follow_up_date/notes to the linked
    Calendar event. No-ops quietly if Google isn't configured; logs and
    swallows API errors so a Calendar outage never blocks a pipeline
    mutation."""
    from app.services.google import auth as google_auth  # local import: avoids a hard dependency at module load
    from app.services.google import calendar_sync

    if not google_auth.is_configured():
        return
    try:
        event_id = calendar_sync.upsert_follow_up_event(google_auth.calendar_service(), entry)
        entry.calendar_event_id = event_id
        log_activity(db, entry.lead_uid, ActivityEventType.CALENDAR_SYNC, source, field_name="calendar_event_id", new_value=event_id)
    except Exception:
        logger.exception("Calendar sync failed for lead %s", entry.lead_uid)


def push_to_sheet(db: Session, entry: MasterLogEntry, source: ActivitySource) -> None:
    """Best-effort push of entry to the Master Log V2 sheet. Only called
    for changes that did NOT originate from a Sheet-edit webhook, so a
    Sheet edit -> Postgres update -> Sheet push cycle can't loop."""
    if source == ActivitySource.WEBHOOK_SHEET:
        return
    from app.services.google import auth as google_auth
    from app.services.google import sheets_sync
    from app.config import get_settings

    settings = get_settings()
    if not google_auth.is_configured() or not settings.google_master_log_sheet_id:
        return
    try:
        sheets_sync.push_master_log_entry(google_auth.sheets_service(), settings.google_master_log_sheet_id, entry)
        log_activity(db, entry.lead_uid, ActivityEventType.SHEET_SYNC, source, field_name="sheet_push")
    except Exception:
        logger.exception("Sheet push failed for lead %s", entry.lead_uid)


def create_lead(
    db: Session, *, business_name: str, co_broker: CoBroker, lead_uid=None, source: ActivitySource = ActivitySource.API, **fields
) -> MasterLogEntry:
    status = fields.pop("status", None) or MasterLogStatus.NEW_LEAD
    entry_kwargs = dict(business_name=business_name, co_broker=co_broker, status=status, **fields)
    if lead_uid is not None:
        # Preserve a caller-assigned UUID (e.g. Apps Script doPost already
        # wrote this UUID into Column A) so Sheets and Postgres stay keyed
        # on the same lead_uid instead of diverging.
        entry_kwargs["lead_uid"] = lead_uid
    entry = MasterLogEntry(**entry_kwargs)
    db.add(entry)
    db.flush()
    db.add(StatusHistory(lead_uid=entry.lead_uid, from_status=None, to_status=status))
    log_activity(db, entry.lead_uid, ActivityEventType.CREATED, source, new_value=business_name)

    if entry.follow_up_date is not None:
        sync_calendar(db, entry, source)
    push_to_sheet(db, entry, source)

    db.commit()
    db.refresh(entry)
    return entry


def update_lead_status(
    db: Session, entry: MasterLogEntry, new_status: MasterLogStatus, source: ActivitySource = ActivitySource.API
) -> MasterLogEntry:
    if new_status != entry.status:
        db.add(StatusHistory(lead_uid=entry.lead_uid, from_status=entry.status, to_status=new_status))
        log_activity(
            db, entry.lead_uid, ActivityEventType.STATUS_CHANGE, source,
            field_name="status", old_value=entry.status.value, new_value=new_status.value,
        )
        entry.status = new_status
        db.commit()
        db.refresh(entry)
    return entry


def update_lead_fields(
    db: Session, entry: MasterLogEntry, updates: dict, source: ActivitySource = ActivitySource.API
) -> MasterLogEntry:
    """Apply a dict of field -> new_value updates to a lead, logging one
    LeadActivityEvent per changed field, routing status changes through
    update_lead_status (so StatusHistory / the hazard engine still see
    it), and re-syncing Calendar + pushing to Sheets exactly once if any
    of the changed fields warrant it. A status mutation always re-syncs
    Calendar (the event description includes current status -- see
    calendar_sync.py -- so it shouldn't go stale until notes/follow-up
    next happen to change too), same as notes/follow_up_date."""
    new_status = updates.pop("status", None)
    calendar_dirty = bool(new_status is not None and new_status != entry.status)

    for field, new_value in updates.items():
        old_value = getattr(entry, field, None)
        if old_value == new_value:
            continue
        event_type = _EVENT_TYPE_BY_FIELD.get(field, ActivityEventType.FIELD_CHANGE)
        log_activity(db, entry.lead_uid, event_type, source, field_name=field, old_value=old_value, new_value=new_value)
        setattr(entry, field, new_value)
        if field in _CALENDAR_TRIGGER_FIELDS:
            calendar_dirty = True

    db.commit()

    if new_status is not None:
        update_lead_status(db, entry, new_status, source)

    if calendar_dirty:
        sync_calendar(db, entry, source)
        db.commit()

    push_to_sheet(db, entry, source)
    db.commit()  # push_to_sheet's own log_activity(SHEET_SYNC) call must be committed here --
    # unlike create_lead (which already commits after its push_to_sheet call), this function
    # had no commit after it, so that activity-log row was silently discarded whenever the
    # caller's session closed without another commit happening to catch it afterward.

    db.refresh(entry)
    return entry


def convert_or_update_silo_candidate(
    db: Session,
    candidate: SiloCandidate,
    new_status: SiloCandidateStatus,
    co_broker: CoBroker | None = None,
    source: ActivitySource = ActivitySource.API,
) -> SiloCandidate:
    if new_status == SiloCandidateStatus.CONVERTED and candidate.converted_lead_uid is None:
        if co_broker is None:
            raise ValueError("co_broker is required to convert a candidate into a lead")

        lead = create_lead(
            db,
            business_name=candidate.company_name,
            co_broker=co_broker,
            contact_name=candidate.contact_name,
            phone=candidate.phone,
            email=candidate.email,
            notes=f"Converted from silo candidate ({candidate.silo.value}). {candidate.notes or ''}".strip(),
            dossier_drive_link=candidate.dossier_drive_link,
            source_silo=candidate.silo,
            source_channel=candidate.origin_source,
            source=source,
        )
        candidate.converted_lead_uid = lead.lead_uid

    candidate.status = new_status
    db.commit()
    db.refresh(candidate)
    return candidate
