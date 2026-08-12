"""Shared pipeline mutations used by both the JSON API routers and the
Apps Script webhook handlers, so lead creation / status transitions /
silo conversion only happen in one place."""

from sqlalchemy.orm import Session

from app.models.enums import CoBroker, MasterLogStatus, SiloCandidateStatus
from app.models.orm import MasterLogEntry, SiloCandidate, StatusHistory


def create_lead(
    db: Session, *, business_name: str, co_broker: CoBroker, lead_uid=None, **fields
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
    db.commit()
    db.refresh(entry)
    return entry


def update_lead_status(db: Session, entry: MasterLogEntry, new_status: MasterLogStatus) -> MasterLogEntry:
    if new_status != entry.status:
        db.add(StatusHistory(lead_uid=entry.lead_uid, from_status=entry.status, to_status=new_status))
        entry.status = new_status
        db.commit()
        db.refresh(entry)
    return entry


def convert_or_update_silo_candidate(
    db: Session, candidate: SiloCandidate, new_status: SiloCandidateStatus, co_broker: CoBroker | None = None
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
            notes=f"Converted from silo candidate ({candidate.silo.value}). {candidate.notes or ''}".strip(),
            dossier_drive_link=candidate.dossier_drive_link,
        )
        candidate.converted_lead_uid = lead.lead_uid

    candidate.status = new_status
    db.commit()
    db.refresh(candidate)
    return candidate
