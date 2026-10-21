"""Bidirectional sync between Postgres and the Master Log V2 spreadsheet
(Master Log V2 tab + the 9 macro silo tabs).

Column layout below is confirmed against the REAL, live spreadsheet
(downloaded and parsed directly, not inferred -- a prior version of this
module and of apps_script/Code.gs's ML_COL_* constants had drifted from
the actual sheet entirely; that mismatch was caught before either one was
ever run against real data, see git history). The original 22 confirmed
columns run A..V; columns W (Original Funded Amount) and X (Funded Date)
are new, appended at the end rather than inserted, so none of the
confirmed A..V indices had to shift. Funded Date accepts either an exact
"M/D/YYYY" (most deals) or a bare "M/YYYY" when the exact day was never
recorded (plenty of real deals funded before this was tracked) -- see
_parse_funded_date and DatePrecision's docstring for how that's told
apart downstream. Three of the original columns are deliberately left
unmapped rather than guessed at:
  - Column B "Date Added" -- not modeled on MasterLogEntry at all.
  - Column S "Event ID" -- holds a human-readable flag (observed value:
    "Synced"), NOT a literal Google Calendar event ID the way
    MasterLogEntry.calendar_event_id does. Mapping it directly would
    silently overwrite that flag with an opaque event-ID string across
    every existing row. Left alone until confirmed how it should map.
  - Column T -- unlabeled, empty in every sampled row. Purpose unknown;
    left alone rather than guessed at.
  - There is no separate "Dossier Link" column in the real sheet at all
    (only Financials Link and Transcripts Link) -- so
    MasterLogEntry.dossier_drive_link does NOT round-trip to the Sheet;
    it's still a usable field via the API/dashboard, just not Sheet-synced.

All three unmapped columns still round-trip safely via `extra_columns`
(JSONB) -- row_to_master_log_fields captures any unmapped column's value
by letter on pull, and master_log_entry_to_row overlays it back on push,
so a value already in one of those columns is preserved, never blanked,
even though this module never actively reads or writes it.

See apps_script/Code.gs's matching ML_COL_* constants, which must be kept
in lockstep with MASTER_LOG_COLUMN_FIELDS below if either one changes.
Column A is used on both sheets as the anchor UUID column so rows can be
matched between Sheets and Postgres without relying on row position.
"""

import datetime as dt
import logging
import uuid

from sqlalchemy.orm import Session

from app.models.enums import ActivitySource, CoBroker, DatePrecision, MasterLogStatus, SiloCandidateStatus, SiloName
from app.models.orm import MasterLogEntry, SiloCandidate
from app.services import pipeline

logger = logging.getLogger("brainboard.sheets_sync")

MASTER_LOG_SHEET_NAME = "Master Log V2"
MASTER_LOG_RANGE = f"{MASTER_LOG_SHEET_NAME}!A2:X"

# column index (0-based, A=0) -> MasterLogEntry field name. Indices not
# listed here fall into extra_columns, keyed by their sheet column letter
# (see module docstring for B/S/T specifically -- deliberately unmapped).
MASTER_LOG_COLUMN_FIELDS: dict[int, str] = {
    0: "lead_uid",  # A -- UUID
    # 1: Date Added (B) -- not modeled, preserved via extra_columns
    2: "state",  # C
    3: "contact_name",  # D -- sheet header reads "Owner Name"
    4: "business_name",  # E
    5: "phone",  # F
    6: "email",  # G
    7: "annual_revenue",  # H -- sheet header reads "Revenue"
    8: "co_broker",  # I
    9: "status",  # J
    10: "follow_up_date",  # K
    11: "notes",  # L
    12: "lender",  # M
    13: "payment_amt",  # N
    14: "payment_freq",  # O
    15: "current_balance",  # P
    16: "open_positions",  # Q
    17: "credit_score",  # R
    # 18: Event ID (S) -- human flag ("Synced"), not calendar_event_id; preserved via extra_columns
    # 19: unlabeled (T) -- unknown purpose, preserved via extra_columns
    20: "financials_link",  # U
    21: "transcripts_link",  # V
    22: "original_funded_amount",  # W -- new, appended (see module docstring)
    23: "funded_date",  # X -- new, appended; "M/D/YYYY" or "M/YYYY" (see _parse_funded_date)
}
MASTER_LOG_COLUMN_COUNT = 24  # A..X
_FLOAT_FIELDS = {"annual_revenue", "payment_amt", "current_balance", "original_funded_amount"}
_INT_FIELDS = {"open_positions", "credit_score"}

SILO_RANGE_SUFFIX = "!A2:H"
SILO_COLUMN_FIELDS: dict[int, str] = {
    0: "candidate_uid",  # A
    1: "company_name",  # B -- note: spec fixes Phone at Col C below
    2: "phone",  # C
    3: "source_reference",  # D
    4: "notes",  # E
    5: "dossier_drive_link",  # F
    6: "score",  # G
    7: "status",  # H
}
SILO_COLUMN_COUNT = 8


def _col_letter(index: int) -> str:
    return chr(ord("A") + index)


# Historical values that still exist verbatim in the real, live sheet from
# before this session's renames (confirmed by actually downloading and
# parsing it, not assumed) -- 75 of 353 real rows still say "Docs Owed"
# and 1 still says "Jorge". Aliased on read so pulling the real sheet
# doesn't fail on >20% of real leads just because nobody has gone back
# and manually fixed every historical cell. New rows get the canonical
# value via the Sheet's dropdown validation; this is purely a read-side
# compatibility shim for rows written before the rename.
_STATUS_ALIASES = {"Docs Owed": MasterLogStatus.DOCS_OWED}
_CO_BROKER_ALIASES = {"Jorge": CoBroker.BOULA}  # matches the Jorge->Boula remap already applied in Postgres


def _parse_date(value: str) -> dt.datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _parse_funded_date(value: str) -> tuple[dt.date | None, DatePrecision | None]:
    """Funded Date (W) is written either as an exact "M/D/YYYY"/"YYYY-MM-DD"
    (most deals, entered close to when they funded) or a bare "M/YYYY"/
    "YYYY-MM" when only the month is remembered (common for backfilling
    deals that funded before this column existed). The month-only forms
    are stored as the 1st of that month with precision=MONTH so nothing
    downstream mistakes that placeholder day for a real one."""
    if not value:
        return None, None
    value = value.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).date(), DatePrecision.EXACT
        except ValueError:
            continue
    for fmt in ("%m/%Y", "%Y-%m"):
        try:
            parsed = dt.datetime.strptime(value, fmt).date()
            return parsed.replace(day=1), DatePrecision.MONTH
        except ValueError:
            continue
    return None, None


def _format_funded_date(funded_date: dt.date | None, precision: DatePrecision | None) -> str:
    if funded_date is None or precision is None:
        return ""
    return funded_date.strftime("%m/%d/%Y") if precision == DatePrecision.EXACT else funded_date.strftime("%m/%Y")


def row_to_master_log_fields(row: list[str]) -> dict:
    padded = row + [""] * (MASTER_LOG_COLUMN_COUNT - len(row))
    fields: dict = {"extra_columns": {}}
    for index, value in enumerate(padded[:MASTER_LOG_COLUMN_COUNT]):
        field_name = MASTER_LOG_COLUMN_FIELDS.get(index)
        if field_name is None:
            if value:
                fields["extra_columns"][_col_letter(index)] = value
            continue
        if field_name == "lead_uid":
            fields["lead_uid"] = uuid.UUID(value) if value else None
        elif field_name == "co_broker":
            fields["co_broker"] = (_CO_BROKER_ALIASES.get(value) or CoBroker(value)) if value else None
        elif field_name == "status":
            fields["status"] = (_STATUS_ALIASES.get(value) or MasterLogStatus(value)) if value else MasterLogStatus.NEW_LEAD
        elif field_name == "follow_up_date":
            fields["follow_up_date"] = _parse_date(value)
        elif field_name == "funded_date":
            if value:
                funded_date, precision = _parse_funded_date(value)
                if funded_date is None:
                    logger.warning("Unparseable funded_date value %r -- stored as null", value)
                fields["funded_date"] = funded_date
                fields["funded_date_precision"] = precision
            else:
                fields["funded_date"] = None
                fields["funded_date_precision"] = None
        elif field_name in _FLOAT_FIELDS:
            # Field-level tolerance, not row-level: 353 rows of months of
            # manual entry turned up real free-text garbage in numeric
            # cells (e.g. "1 Payment", "CC%") -- one bad numeric cell
            # should drop just that field, not the whole row's business
            # name/phone/notes along with it.
            try:
                fields[field_name] = float(value.replace(",", "").replace("$", "")) if value else None
            except ValueError:
                logger.warning("Unparseable %s value %r -- stored as null", field_name, value)
                fields[field_name] = None
        elif field_name in _INT_FIELDS:
            try:
                fields[field_name] = int(float(value)) if value else None
            except ValueError:
                logger.warning("Unparseable %s value %r -- stored as null", field_name, value)
                fields[field_name] = None
        else:
            fields[field_name] = value or None
    return fields


def master_log_entry_to_row(entry: MasterLogEntry) -> list[str]:
    row = [""] * MASTER_LOG_COLUMN_COUNT
    row[0] = str(entry.lead_uid)
    # row[1] (Date Added, B) intentionally left blank here -- restored below from extra_columns if present
    row[2] = entry.state or ""
    row[3] = entry.contact_name or ""
    row[4] = entry.business_name or ""
    row[5] = entry.phone or ""
    row[6] = entry.email or ""
    row[7] = str(entry.annual_revenue) if entry.annual_revenue is not None else ""
    row[8] = entry.co_broker.value if entry.co_broker else ""
    row[9] = entry.status.value if entry.status else ""
    row[10] = entry.follow_up_date.strftime("%Y-%m-%d") if entry.follow_up_date else ""
    row[11] = entry.notes or ""
    row[12] = entry.lender or ""
    row[13] = str(entry.payment_amt) if entry.payment_amt is not None else ""
    row[14] = entry.payment_freq or ""
    row[15] = str(entry.current_balance) if entry.current_balance is not None else ""
    row[16] = str(entry.open_positions) if entry.open_positions is not None else ""
    row[17] = str(entry.credit_score) if entry.credit_score is not None else ""
    # row[18] (Event ID, S) and row[19] (unlabeled, T) intentionally left blank here -- restored below
    row[20] = entry.financials_link or ""
    row[21] = entry.transcripts_link or ""
    row[22] = str(entry.original_funded_amount) if entry.original_funded_amount is not None else ""
    row[23] = _format_funded_date(entry.funded_date, entry.funded_date_precision)
    # Note: entry.dossier_drive_link has no column to write to -- see module docstring.
    for letter, value in (entry.extra_columns or {}).items():
        index = ord(letter.upper()) - ord("A")
        if 0 <= index < MASTER_LOG_COLUMN_COUNT:
            row[index] = value
    return row


def row_to_silo_fields(row: list[str]) -> dict:
    padded = row + [""] * (SILO_COLUMN_COUNT - len(row))
    fields: dict = {}
    for index, value in enumerate(padded[:SILO_COLUMN_COUNT]):
        field_name = SILO_COLUMN_FIELDS[index]
        if field_name == "candidate_uid":
            fields["candidate_uid"] = uuid.UUID(value) if value else None
        elif field_name == "score":
            fields["score"] = float(value) if value else None
        elif field_name == "status":
            fields["status"] = SiloCandidateStatus(value) if value else SiloCandidateStatus.PENDING
        else:
            fields[field_name] = value or None
    return fields


def silo_candidate_to_row(candidate: SiloCandidate) -> list[str]:
    return [
        str(candidate.candidate_uid),
        candidate.company_name or "",
        candidate.phone or "",
        candidate.source_reference or "",
        candidate.notes or "",
        candidate.dossier_drive_link or "",
        str(candidate.score) if candidate.score is not None else "",
        candidate.status.value if candidate.status else "",
    ]


def pull_master_log(db: Session, service, sheet_id: str) -> dict:
    """Upsert MasterLogEntry rows from the sheet into Postgres, routed
    through pipeline.create_lead / update_lead_fields (source=
    WEBHOOK_SHEET) so a full-sheet pull gets the same activity logging,
    StatusHistory, and Calendar sync as a single-row onMasterLogEdit
    webhook -- the Sheet is the source of truth regardless of which sync
    path picked up the change. Rows missing a UUID in Column A are
    assigned one and written back.

    A single malformed row (bad UUID, unparseable enum value, etc.) is
    logged and skipped rather than aborting the whole run -- real
    production sheets accumulate messy rows over months of manual entry,
    and one bad row must not block the other few hundred behind it.
    Returns {"synced": int, "failed": [(row_number, error), ...]}."""
    result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=MASTER_LOG_RANGE).execute()
    rows = result.get("values", [])

    updates_needed: list[tuple[int, str]] = []  # (sheet row number, new uuid)
    synced = 0
    failed: list[tuple[int, str]] = []  # (sheet row number, error) -- real production data has messy rows

    for offset, row in enumerate(rows):
        row_number = offset + 2  # header row + 1-indexing
        try:
            if not row or not row[0]:
                new_uid = uuid.uuid4()
                row = [str(new_uid)] + row[1:]
                updates_needed.append((row_number, str(new_uid)))

            fields = row_to_master_log_fields(row)
            lead_uid = fields.pop("lead_uid")
            entry = db.get(MasterLogEntry, lead_uid)

            if entry is None:
                business_name = fields.pop("business_name", None) or "(unnamed)"
                co_broker = fields.pop("co_broker", None)
                if co_broker is None:
                    logger.warning("Skipping new sheet row %d for %r -- co_broker cell is blank", row_number, business_name)
                    continue
                pipeline.create_lead(
                    db, business_name=business_name, co_broker=co_broker, lead_uid=lead_uid,
                    source=ActivitySource.WEBHOOK_SHEET,
                    **{k: v for k, v in fields.items() if v is not None or k == "extra_columns"},
                )
            else:
                updates = {k: v for k, v in fields.items() if v is not None or k == "extra_columns"}
                pipeline.update_lead_fields(db, entry, updates, source=ActivitySource.WEBHOOK_SHEET)
            synced += 1
        except Exception as exc:  # noqa: BLE001 -- one malformed row (bad UUID, bad enum value, etc.)
            # must not abort the sync for the other ~350 real rows behind it
            db.rollback()
            logger.exception("Sheet row %d failed to sync -- skipped", row_number)
            failed.append((row_number, str(exc)))

    for row_number, new_uid in updates_needed:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{MASTER_LOG_SHEET_NAME}!A{row_number}",
            valueInputOption="RAW",
            body={"values": [[new_uid]]},
        ).execute()

    return {"synced": synced, "failed": failed}


def push_master_log_entry(service, sheet_id: str, entry: MasterLogEntry) -> None:
    """Write a single MasterLogEntry back to its sheet row, matching on the
    UUID in Column A. Appends a new row if the UUID isn't found."""
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{MASTER_LOG_SHEET_NAME}!A2:A")
        .execute()
    )
    ids = [r[0] if r else "" for r in result.get("values", [])]
    row_values = master_log_entry_to_row(entry)

    if str(entry.lead_uid) in ids:
        row_number = ids.index(str(entry.lead_uid)) + 2
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{MASTER_LOG_SHEET_NAME}!A{row_number}:X{row_number}",
            valueInputOption="RAW",
            body={"values": [row_values]},
        ).execute()
    else:
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=MASTER_LOG_RANGE,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row_values]},
        ).execute()


def pull_silo(db: Session, service, sheet_id: str, silo: SiloName) -> dict:
    """Same per-row error isolation as pull_master_log -- one malformed
    silo row must not abort the sync for the rest of that tab. Returns
    {"synced": int, "failed": [(row_number, error), ...]}."""
    range_name = f"{silo.value}{SILO_RANGE_SUFFIX}"
    result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=range_name).execute()
    rows = result.get("values", [])

    updates_needed: list[tuple[int, str]] = []
    synced = 0
    failed: list[tuple[int, str]] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2
        try:
            if not row or not row[0]:
                new_uid = uuid.uuid4()
                row = [str(new_uid)] + row[1:]
                updates_needed.append((row_number, str(new_uid)))

            fields = row_to_silo_fields(row)
            candidate_uid = fields.pop("candidate_uid")
            candidate = db.get(SiloCandidate, candidate_uid)
            if candidate is None:
                candidate = SiloCandidate(
                    candidate_uid=candidate_uid, silo=silo, company_name=fields.get("company_name") or "(unnamed)"
                )
                db.add(candidate)
            for key, value in fields.items():
                if value is not None:
                    setattr(candidate, key, value)
            synced += 1
        except Exception as exc:  # noqa: BLE001 -- see pull_master_log's matching comment
            db.rollback()
            logger.exception("Silo %s row %d failed to sync -- skipped", silo.value, row_number)
            failed.append((row_number, str(exc)))

    db.commit()

    for row_number, new_uid in updates_needed:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{silo.value}!A{row_number}",
            valueInputOption="RAW",
            body={"values": [[new_uid]]},
        ).execute()

    return {"synced": synced, "failed": failed}


def sync_all_silos(db: Session, service, sheet_id: str) -> dict[str, dict]:
    return {silo.value: pull_silo(db, service, sheet_id, silo) for silo in SiloName}
