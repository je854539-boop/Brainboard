"""Bidirectional Calendar sync, keyed on the lead_uid embedded in each
event's description.

PUSH (Postgres -> Calendar): `upsert_follow_up_event`, triggered by Master
Log V2 Column K (follow-up date) / Column L (notes) edits via
`onMasterLogEdit` in the Apps Script (see apps_script/Code.gs) ->
`/webhooks/master-log-edit` -> here.

PULL (Calendar -> Postgres): `pull_calendar_updates`. The real, live
calendar (confirmed by actually reading hundreds of real events, not
assumed) is NOT just a mirror of the Sheet -- brokers type running call
notes and deal terms (lender, payment amount/frequency, balance, open
positions, credit score) directly into the event description during or
right after a call, and that detail frequently never gets typed back
into the Sheet. Without this direction, Brainboard would treat the Sheet
as the complete record and silently miss the most current, and often
the only, account of a deal's actual status.

HONESTY NOTE on event format: these are real production calendar events,
not ones Brainboard created, so the description format below is reverse-
engineered from reading them directly -- it is not a guess:

    UUID: <lead_uid>

    --- NOTES ---
    <free text, possibly multiple dated paragraphs appended over time>

    --- LEAD DATA ---
    Owner: <contact_name>
    State: <state>
    Revenue: <annual_revenue>
    Lender: <lender>
    Pay Amt: <payment_amt>
    Pay Freq: <payment_freq>
    Balance: <current_balance>
    Open Pos: <open_positions>
    Credit: <credit_score>

A real, non-trivial share of older events (pre-dating this format, titled
"FOLLOW UP: ..." or carrying a "RAW HEADSET LOG HISTORY" banner) have no
UUID at all -- pull_calendar_updates skips these; they can't be matched to
a lead_uid and were never going to be write-back candidates for this sync.

Matching an existing event: a fresh `extendedProperties.private.lead_uid`
match (events this module itself created) is tried first, but a full-text
`q=` search for the UUID is also tried and preferred when it hits a real
event already carrying the team's own data -- real historical events are
never tagged with extendedProperties (they were never created by this
code), so relying on extendedProperties alone would silently create a
duplicate event for every existing lead instead of updating the real one.
"""

import datetime as dt
import logging
import re

from app.config import get_settings
from app.models.orm import MasterLogEntry

logger = logging.getLogger("brainboard.calendar_sync")

_LEAD_DATA_FIELDS = {
    "Owner": "contact_name",
    "State": "state",
    "Revenue": "annual_revenue",
    "Lender": "lender",
    "Pay Amt": "payment_amt",
    "Pay Freq": "payment_freq",
    "Balance": "current_balance",
    "Open Pos": "open_positions",
    "Credit": "credit_score",
}
_FLOAT_FIELDS = {"annual_revenue", "payment_amt", "current_balance"}
_INT_FIELDS = {"open_positions", "credit_score"}


def _find_event_by_lead_uid(service, calendar_id: str, lead_uid: str) -> dict | None:
    response = (
        service.events()
        .list(calendarId=calendar_id, privateExtendedProperty=f"lead_uid={lead_uid}", maxResults=1)
        .execute()
    )
    items = response.get("items", [])
    if items:
        return items[0]

    # Fallback: real team-created events carry the UUID in the description
    # text, not as an extended property -- a q= search finds the candidate,
    # then we confirm it's a real match (q= is free-text, not exact).
    response = service.events().list(calendarId=calendar_id, q=lead_uid, maxResults=5).execute()
    for item in response.get("items", []):
        if lead_uid in (item.get("description") or ""):
            return item
    return None


def upsert_follow_up_event(service, lead: MasterLogEntry) -> str | None:
    """Create/update the Calendar event for this lead's follow-up date.
    Returns the Calendar event ID, or None if there's no follow-up date to
    schedule (existing event is deleted in that case). Description format
    matches the team's own existing convention (see module docstring) so
    cutting over doesn't change what anyone's calendar looks like."""
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

    lead_data_lines = [
        f"Owner: {lead.contact_name or ''}",
        f"State: {lead.state or ''}",
        f"Revenue: {lead.annual_revenue if lead.annual_revenue is not None else ''}",
        f"Lender: {lead.lender or ''}",
        f"Pay Amt: {lead.payment_amt if lead.payment_amt is not None else ''}",
        f"Pay Freq: {lead.payment_freq or ''}",
        f"Balance: {lead.current_balance if lead.current_balance is not None else ''}",
        f"Open Pos: {lead.open_positions if lead.open_positions is not None else ''}",
        f"Credit: {lead.credit_score if lead.credit_score is not None else ''}",
    ]
    body = {
        "summary": f"{lead.business_name} | {lead.phone or ''}",
        "description": (
            f"UUID: {lead_uid}\n\n"
            f"--- NOTES ---\n{lead.notes or ''}\n\n"
            f"--- LEAD DATA ---\n" + "\n".join(lead_data_lines)
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


_UUID_RE = re.compile(r"UUID:\s*([0-9a-fA-F-]{36})")
_NOTES_RE = re.compile(r"---\s*NOTES\s*---\s*\n(.*?)\n\s*---\s*LEAD DATA\s*---", re.DOTALL)
_LEAD_DATA_RE = re.compile(r"---\s*LEAD DATA\s*---\s*\n(.*)", re.DOTALL)


def parse_event_description(description: str) -> dict | None:
    """Reverses the format in the module docstring back into a field dict.
    Returns None if the description carries no UUID -- an older/legacy
    event (FOLLOW UP:/RAW HEADSET LOG HISTORY style) that can't be matched
    to a lead_uid at all, not a parse failure."""
    if not description:
        return None
    uuid_match = _UUID_RE.search(description)
    if not uuid_match:
        return None

    fields: dict = {"lead_uid": uuid_match.group(1)}

    notes_match = _NOTES_RE.search(description)
    if notes_match:
        fields["notes"] = notes_match.group(1).strip()

    lead_data_match = _LEAD_DATA_RE.search(description)
    if lead_data_match:
        for line in lead_data_match.group(1).splitlines():
            if ":" not in line:
                continue
            label, _, raw_value = line.partition(":")
            label, raw_value = label.strip(), raw_value.strip()
            field_name = _LEAD_DATA_FIELDS.get(label)
            if field_name is None or not raw_value:
                continue
            if field_name in _FLOAT_FIELDS:
                try:
                    fields[field_name] = float(raw_value.replace(",", "").replace("$", ""))
                except ValueError:
                    logger.warning("Calendar event %s: unparseable %s value %r -- skipped", fields["lead_uid"], field_name, raw_value)
            elif field_name in _INT_FIELDS:
                try:
                    fields[field_name] = int(float(raw_value))
                except ValueError:
                    logger.warning("Calendar event %s: unparseable %s value %r -- skipped", fields["lead_uid"], field_name, raw_value)
            else:
                fields[field_name] = raw_value

    return fields


def pull_calendar_updates(db, service, calendar_id: str, since_minutes: int) -> dict:
    """Finds events updated in the last `since_minutes`, parses the ones
    that carry a lead_uid, and merges their notes/lead-data into Postgres
    via pipeline.update_lead_fields(source=WEBHOOK_CALENDAR) -- that
    source both skips re-writing the same Calendar event right back (see
    sync_calendar's check) and still pushes the merge on to the Sheet, the
    same way a Sheet-originated edit pushes on to Calendar.

    No persisted sync cursor -- each poll just re-scans a trailing window
    a little wider than the poll interval, so a missed cycle (restart,
    transient API error) self-heals on the next one instead of silently
    losing updates. Reprocessing an event whose fields haven't actually
    changed is a safe no-op (update_lead_fields already skips unchanged
    fields).

    Returns {"synced": int, "skipped_no_uuid": int, "failed": [(event_id, error), ...]}."""
    from app.models.enums import ActivitySource
    from app.models.orm import MasterLogEntry
    from app.services import pipeline

    updated_min = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=since_minutes)).isoformat()
    response = service.events().list(calendarId=calendar_id, updatedMin=updated_min, singleEvents=True, showDeleted=False).execute()
    events = response.get("items", [])

    synced, skipped_no_uuid = 0, 0
    failed: list[tuple[str, str]] = []

    for event in events:
        event_id = event.get("id", "?")
        try:
            fields = parse_event_description(event.get("description") or "")
            if fields is None:
                skipped_no_uuid += 1
                continue

            lead_uid = fields.pop("lead_uid")
            entry = db.get(MasterLogEntry, lead_uid)
            if entry is None:
                logger.warning("Calendar event %s references unknown lead_uid %s -- skipped", event_id, lead_uid)
                continue

            if fields:
                pipeline.update_lead_fields(db, entry, fields, source=ActivitySource.WEBHOOK_CALENDAR)
            synced += 1
        except Exception as exc:  # noqa: BLE001 -- one bad event must not abort the rest of the poll
            db.rollback()
            logger.exception("Calendar event %s failed to sync -- skipped", event_id)
            failed.append((event_id, str(exc)))

    return {"synced": synced, "skipped_no_uuid": skipped_no_uuid, "failed": failed}


# ---------------------------------------------------------------------------
# Background pull -- must not block the FastAPI event loop
# ---------------------------------------------------------------------------

_pull_task = None


async def _pull_loop() -> None:
    import asyncio

    from app.database import SessionLocal

    while True:
        try:
            settings = get_settings()
            from app.services.google import auth as google_auth

            if google_auth.is_configured() and settings.google_calendar_id:
                db = SessionLocal()
                try:
                    summary = await asyncio.to_thread(
                        pull_calendar_updates,
                        db,
                        google_auth.calendar_service(),
                        settings.google_calendar_id,
                        settings.calendar_pull_window_minutes,
                    )
                    if summary["synced"] or summary["failed"]:
                        logger.info("Calendar pull: %s", summary)
                finally:
                    db.close()
            else:
                logger.info("Calendar pull skipped this cycle -- Google credentials/calendar ID not configured")
        except Exception:  # noqa: BLE001 -- one bad cycle must not kill the loop
            logger.exception("Calendar pull failed -- will retry next cycle")
        await asyncio.sleep(get_settings().calendar_pull_interval_seconds)


def reconcile_all_events(db, service, calendar_id: str) -> dict:
    """One-time (safely re-runnable) full-history scan of every event ever
    created on the calendar -- not the ongoing updatedMin-windowed
    pull_calendar_updates above, which only ever looks at a trailing
    window and would take months to work through a backlog that already
    exists today. Cross-references each event against Postgres:

      - UUID in the description, lead_uid matches a real MasterLogEntry
        -> high-confidence, auto-merged: notes are combined via
        notes_merge.merge_notes (superset-aware, not a blind overwrite)
        and any lead-data fields present are applied the same way
        pull_calendar_updates does, tagged WEBHOOK_CALENDAR so the loop-
        prevention check in pipeline.sync_calendar still holds.
      - UUID present but no matching lead in Postgres -> "orphaned_uuid":
        reported only, never auto-created -- a UUID with no row behind
        it is more likely a deleted/renamed lead than a new one worth
        materializing sight-unseen.
      - No UUID at all (legacy event, pre-dates this convention) -> an
        exact digits-only phone match against every lead's phone column
        is attempted from the event summary ("{business_name} | {phone}"
        -- see upsert_follow_up_event). A single match is reported as
        "fallback_match_phone" for a HUMAN to confirm; this function
        deliberately never auto-merges or writes back on a phone-only
        match -- "looks like the same merchant" isn't the same
        confidence as a UUID actually proving it. Ambiguous (2+ matches)
        or no match at all both count as "unmatched".

    Returns counts plus the actual event/lead pairs for the orphan and
    fallback buckets so a human reviews real cases, not an opaque total."""
    import re as _re

    from sqlalchemy import select

    from app.models.enums import ActivitySource
    from app.services import notes_merge, pipeline

    leads_by_phone: dict[str, list[str]] = {}
    for lead_uid, phone in db.execute(select(MasterLogEntry.lead_uid, MasterLogEntry.phone)).all():
        digits = _re.sub(r"\D", "", phone or "")
        if digits:
            leads_by_phone.setdefault(digits, []).append(str(lead_uid))

    matched_by_uuid = 0
    orphaned_uuid: list[dict] = []
    fallback_match_phone: list[dict] = []
    unmatched = 0
    failed: list[tuple[str, str]] = []

    page_token = None
    while True:
        response = service.events().list(
            calendarId=calendar_id, singleEvents=True, showDeleted=False, maxResults=250, pageToken=page_token,
        ).execute()

        for event in response.get("items", []):
            event_id = event.get("id", "?")
            try:
                parsed = parse_event_description(event.get("description") or "")

                if parsed is not None:
                    lead_uid = parsed.pop("lead_uid")
                    entry = db.get(MasterLogEntry, lead_uid)
                    if entry is None:
                        orphaned_uuid.append({"event_id": event_id, "lead_uid": lead_uid, "summary": event.get("summary", "")})
                        continue

                    calendar_notes = parsed.pop("notes", None)
                    if calendar_notes:
                        merged, _diverged = notes_merge.merge_notes(entry.notes, calendar_notes)
                        if merged != (entry.notes or ""):
                            parsed["notes"] = merged
                    if parsed:
                        pipeline.update_lead_fields(db, entry, parsed, source=ActivitySource.WEBHOOK_CALENDAR)
                    matched_by_uuid += 1
                    continue

                summary = event.get("summary") or ""
                phone_match = _re.search(r"\|\s*(.+)$", summary)
                digits = _re.sub(r"\D", "", phone_match.group(1)) if phone_match else ""
                candidates = leads_by_phone.get(digits, []) if digits else []
                if len(candidates) == 1:
                    fallback_match_phone.append({"event_id": event_id, "lead_uid": candidates[0], "summary": summary})
                else:
                    unmatched += 1
            except Exception as exc:  # noqa: BLE001 -- one bad event must not abort a scan of the whole history
                db.rollback()
                logger.exception("Calendar event %s failed during reconciliation -- skipped", event_id)
                failed.append((event_id, str(exc)))

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return {
        "matched_by_uuid": matched_by_uuid,
        "orphaned_uuid": orphaned_uuid,
        "fallback_match_phone": fallback_match_phone,
        "unmatched": unmatched,
        "failed": failed,
    }


def start_background_pull():
    """Off by default (calendar_continuous_pull_enabled) -- unlike
    brain/enrichment, this hits a real external API, so it stays opt-in
    until Google credentials are actually configured (same posture as
    river_surveillance.start_background_polling)."""
    import asyncio

    global _pull_task
    if not get_settings().calendar_continuous_pull_enabled:
        logger.info("Calendar continuous pull disabled (set CALENDAR_CONTINUOUS_PULL_ENABLED=true to activate)")
        return None
    if _pull_task is None or _pull_task.done():
        _pull_task = asyncio.create_task(_pull_loop())
        logger.info("Calendar background pull started (interval=%ss, window=%smin)",
                    get_settings().calendar_pull_interval_seconds, get_settings().calendar_pull_window_minutes)
    return _pull_task


def stop_background_pull() -> None:
    global _pull_task
    if _pull_task is not None:
        _pull_task.cancel()
        _pull_task = None
        logger.info("Calendar background pull stopped")
