"""Bidirectional sync between Postgres and the Master Log V2 spreadsheet
(Master Log V2 tab + the 9 macro silo tabs).

Column layout below is the default mapping and MUST be confirmed against
the live spreadsheet's header row before going to production -- only
Column K (follow-up date), Column L (notes), and Column X (dossier link)
are fixed by the stated spec; the remaining Master Log V2 columns are a
reasonable default that ops should verify. Column A is used on both sheets
as the anchor UUID column so rows can be matched between Sheets and
Postgres without relying on row position.
"""

import datetime as dt
import uuid

from sqlalchemy.orm import Session

from app.models.enums import CoBroker, MasterLogStatus, SiloCandidateStatus, SiloName
from app.models.orm import MasterLogEntry, SiloCandidate

MASTER_LOG_SHEET_NAME = "Master Log V2"
MASTER_LOG_RANGE = f"{MASTER_LOG_SHEET_NAME}!A2:X"

# column index (0-based, A=0) -> MasterLogEntry field name. Indices not
# listed here fall into extra_columns, keyed by their sheet column letter.
MASTER_LOG_COLUMN_FIELDS: dict[int, str] = {
    0: "lead_uid",  # A
    1: "business_name",  # B
    2: "contact_name",  # C
    3: "phone",  # D
    4: "email",  # E
    5: "co_broker",  # F
    6: "status",  # G
    7: "loan_amount_requested",  # H
    10: "follow_up_date",  # K
    11: "notes",  # L
    23: "dossier_drive_link",  # X
}
MASTER_LOG_COLUMN_COUNT = 24  # A..X

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


def _parse_date(value: str) -> dt.datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


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
            fields["co_broker"] = CoBroker(value) if value else None
        elif field_name == "status":
            fields["status"] = MasterLogStatus(value) if value else MasterLogStatus.NEW_LEAD
        elif field_name == "follow_up_date":
            fields["follow_up_date"] = _parse_date(value)
        elif field_name == "loan_amount_requested":
            fields["loan_amount_requested"] = float(value) if value else None
        else:
            fields[field_name] = value or None
    return fields


def master_log_entry_to_row(entry: MasterLogEntry) -> list[str]:
    row = [""] * MASTER_LOG_COLUMN_COUNT
    row[0] = str(entry.lead_uid)
    row[1] = entry.business_name or ""
    row[2] = entry.contact_name or ""
    row[3] = entry.phone or ""
    row[4] = entry.email or ""
    row[5] = entry.co_broker.value if entry.co_broker else ""
    row[6] = entry.status.value if entry.status else ""
    row[7] = str(entry.loan_amount_requested) if entry.loan_amount_requested is not None else ""
    row[10] = entry.follow_up_date.strftime("%Y-%m-%d") if entry.follow_up_date else ""
    row[11] = entry.notes or ""
    row[23] = entry.dossier_drive_link or ""
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


def pull_master_log(db: Session, service, sheet_id: str) -> int:
    """Upsert MasterLogEntry rows from the sheet into Postgres. Rows
    missing a UUID in Column A are assigned one and written back."""
    result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=MASTER_LOG_RANGE).execute()
    rows = result.get("values", [])

    updates_needed: list[tuple[int, str]] = []  # (sheet row number, new uuid)
    synced = 0

    for offset, row in enumerate(rows):
        if not row or not row[0]:
            new_uid = uuid.uuid4()
            row = [str(new_uid)] + row[1:]
            updates_needed.append((offset + 2, str(new_uid)))  # +2: header row + 1-indexing

        fields = row_to_master_log_fields(row)
        lead_uid = fields.pop("lead_uid")
        entry = db.get(MasterLogEntry, lead_uid)
        if entry is None:
            entry = MasterLogEntry(lead_uid=lead_uid, business_name=fields.get("business_name") or "(unnamed)")
            db.add(entry)
        for key, value in fields.items():
            if value is not None or key == "extra_columns":
                setattr(entry, key, value)
        synced += 1

    db.commit()

    for row_number, new_uid in updates_needed:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{MASTER_LOG_SHEET_NAME}!A{row_number}",
            valueInputOption="RAW",
            body={"values": [[new_uid]]},
        ).execute()

    return synced


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


def pull_silo(db: Session, service, sheet_id: str, silo: SiloName) -> int:
    range_name = f"{silo.value}{SILO_RANGE_SUFFIX}"
    result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=range_name).execute()
    rows = result.get("values", [])

    updates_needed: list[tuple[int, str]] = []
    synced = 0

    for offset, row in enumerate(rows):
        if not row or not row[0]:
            new_uid = uuid.uuid4()
            row = [str(new_uid)] + row[1:]
            updates_needed.append((offset + 2, str(new_uid)))

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

    db.commit()

    for row_number, new_uid in updates_needed:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{silo.value}!A{row_number}",
            valueInputOption="RAW",
            body={"values": [[new_uid]]},
        ).execute()

    return synced


def sync_all_silos(db: Session, service, sheet_id: str) -> dict[str, int]:
    return {silo.value: pull_silo(db, service, sheet_id, silo) for silo in SiloName}
