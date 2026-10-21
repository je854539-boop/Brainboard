"""Manual sync triggers for the Sheet/Calendar/Postgres triangle.

The background loops (calendar_sync.start_background_pull, and the
onMasterLogEdit webhook) handle the ongoing, incremental direction of
each sync automatically -- these endpoints are for the two things those
loops were never built to do: reconcile a backlog that already exists
(reconcile_all_events scans the ENTIRE calendar history once Google
credentials are live, not just a trailing window) and force an on-demand
full pull when needed. Require Google credentials to be configured --
404s cleanly (not a 500) when they aren't, since this sandbox routinely
runs with none.
"""

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.database import SessionLocal
from app.services.google import auth as google_auth
from app.services.google import calendar_sync, sheets_sync

router = APIRouter(prefix="/api/sync", tags=["sync"])


def _require_google():
    if not google_auth.is_configured():
        raise HTTPException(status_code=404, detail="Google credentials not configured")


@router.post("/calendar/reconcile")
def reconcile_calendar():
    """Full-history scan: every event ever created on the calendar,
    cross-referenced against every lead in Postgres. See
    calendar_sync.reconcile_all_events for exactly how matching works and
    why fallback (no-UUID) matches are reported, not auto-merged."""
    _require_google()
    settings = get_settings()
    db = SessionLocal()
    try:
        return calendar_sync.reconcile_all_events(db, google_auth.calendar_service(), settings.google_calendar_id)
    finally:
        db.close()


@router.post("/calendar/pull")
def pull_calendar(since_minutes: int = 60):
    """On-demand version of the background pull -- same trailing-window
    behavior, triggerable without waiting for the next scheduled cycle."""
    _require_google()
    settings = get_settings()
    db = SessionLocal()
    try:
        return calendar_sync.pull_calendar_updates(db, google_auth.calendar_service(), settings.google_calendar_id, since_minutes)
    finally:
        db.close()


@router.post("/sheet/pull")
def pull_sheet():
    """Full Master Log V2 pull -- every row, upserted into Postgres. See
    sheets_sync.pull_master_log for the per-row error isolation that
    makes this safe to run against a real, messy production sheet."""
    _require_google()
    settings = get_settings()
    db = SessionLocal()
    try:
        return sheets_sync.pull_master_log(db, google_auth.sheets_service(), settings.google_master_log_sheet_id)
    finally:
        db.close()


@router.post("/silos/pull")
def pull_silos():
    """Pull all 9 macro silo tabs in one call. See sheets_sync.sync_all_silos."""
    _require_google()
    settings = get_settings()
    db = SessionLocal()
    try:
        return sheets_sync.sync_all_silos(db, google_auth.sheets_service(), settings.google_master_log_sheet_id)
    finally:
        db.close()
