"""Merges a lead's Sheet-side notes with its Calendar-side notes into one
text, for backfilling the two systems' historical gap (see
google/calendar_sync.py's module docstring -- the live calendar routinely
carries detail that was typed directly into an event and never made it
into the Sheet's Notes column, and vice versa isn't impossible either).

This is NOT the ongoing poll-based sync (pull_calendar_updates already
handles that going forward) -- it's the one-time reconciliation a real
backfill needs, and is deliberately just the merge logic, not something
that writes to the Sheet or Calendar itself. See README/deploy notes for
why that write only happens through the existing, already-tested
pull_master_log / pull_calendar_updates / push_to_sheet / sync_calendar
paths once real credentials are live, not as a one-off script against
production data.
"""


def _normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def merge_notes(sheet_notes: str | None, calendar_notes: str | None) -> tuple[str, bool]:
    """Returns (merged_text, needed_concat_fallback). The common real case
    (confirmed by reading actual duplicate Sheet/Calendar note pairs this
    session) is one side being an exact prefix of the other -- the same
    note, with more dated entries appended on whichever side someone
    happened to update most recently. In that case the longer side IS the
    merge; nothing needs concatenating. Only when the two have genuinely
    diverged (neither contains the other) does this fall back to
    concatenating both under a clear separator, flagged via the second
    return value so a human can sanity-check a real conflict instead of
    it being silently picked one way."""
    sheet_notes = (sheet_notes or "").strip()
    calendar_notes = (calendar_notes or "").strip()

    if not sheet_notes:
        return calendar_notes, False
    if not calendar_notes:
        return sheet_notes, False
    if sheet_notes == calendar_notes:
        return sheet_notes, False

    norm_sheet, norm_cal = _normalize(sheet_notes), _normalize(calendar_notes)
    if norm_cal.startswith(norm_sheet):
        return calendar_notes, False
    if norm_sheet.startswith(norm_cal):
        return sheet_notes, False

    return (
        f"{sheet_notes}\n\n--- MERGED FROM CALENDAR (diverged, not a superset -- review) ---\n{calendar_notes}",
        True,
    )
