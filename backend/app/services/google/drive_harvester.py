"""Automated Drive dossier harvester.

Walks `[Business Name] | [UUID]/FINANCIALS` subfolders under the
configured Drive root, OCRs any new bank/merchant statement (PDF or
image), and runs a lightweight cash-flow extraction pass over the OCR
text. Extracted figures are written to TelemetryEvent so they show up on
the Surveillance & OCR Feed and can be correlated against the lead by
UUID (parsed out of the parent folder name).

The regex-based extraction below is a functional baseline (ending
balance, average daily balance, NSF/overdraft count) -- swap
`extract_financial_signals` for an LLM-based extraction call if you need
deeper statement understanding than keyword/regex parsing gives you.
"""

import io
import re
import uuid

from pdf2image import convert_from_bytes
from PIL import Image
from sqlalchemy.orm import Session

try:
    import pytesseract
except ImportError:  # pragma: no cover - optional at import time
    pytesseract = None

from app.models.enums import TelemetrySource
from app.models.orm import TelemetryEvent

FOLDER_NAME_PATTERN = re.compile(
    r"^(?P<business_name>.+?)\s*\|\s*(?P<lead_uid>[0-9a-fA-F-]{36})$"
)

BALANCE_PATTERN = re.compile(r"(?:ending|closing)\s+balance[:\s]*\$?([\d,]+\.\d{2})", re.IGNORECASE)
AVG_DAILY_BALANCE_PATTERN = re.compile(r"average\s+daily\s+balance[:\s]*\$?([\d,]+\.\d{2})", re.IGNORECASE)
NSF_PATTERN = re.compile(r"(NSF|overdraft)", re.IGNORECASE)


def ocr_bytes(content: bytes, mime_type: str) -> str:
    if pytesseract is None:
        raise RuntimeError("pytesseract is not installed -- OCR unavailable")

    if mime_type == "application/pdf":
        pages: list[Image.Image] = convert_from_bytes(content)
        return "\n".join(pytesseract.image_to_string(page) for page in pages)

    image = Image.open(io.BytesIO(content))
    return pytesseract.image_to_string(image)


def extract_financial_signals(ocr_text: str) -> dict:
    balances = [float(b.replace(",", "")) for b in BALANCE_PATTERN.findall(ocr_text)]
    avg_daily = [float(b.replace(",", "")) for b in AVG_DAILY_BALANCE_PATTERN.findall(ocr_text)]
    return {
        "ending_balance": balances[-1] if balances else None,
        "average_daily_balance": avg_daily[-1] if avg_daily else None,
        "nsf_or_overdraft_mentions": len(NSF_PATTERN.findall(ocr_text)),
        "text_length": len(ocr_text),
    }


def harvest_root_folder(db: Session, service, root_folder_id: str) -> int:
    """List `[Business Name] | [UUID]` lead folders under the Drive root,
    OCR any file inside their FINANCIALS subfolder, and log a
    TelemetryEvent per document. Returns the number of documents
    processed."""
    lead_folders = (
        service.files()
        .list(
            q=f"'{root_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id, name)",
        )
        .execute()
        .get("files", [])
    )

    processed = 0
    for folder in lead_folders:
        match = FOLDER_NAME_PATTERN.match(folder["name"])
        lead_uid = uuid.UUID(match.group("lead_uid")) if match else None

        financials_subfolders = (
            service.files()
            .list(
                q=(
                    f"'{folder['id']}' in parents and name='FINANCIALS' "
                    "and mimeType='application/vnd.google-apps.folder' and trashed=false"
                ),
                fields="files(id, name)",
            )
            .execute()
            .get("files", [])
        )
        if not financials_subfolders:
            continue

        documents = (
            service.files()
            .list(
                q=f"'{financials_subfolders[0]['id']}' in parents and trashed=false",
                fields="files(id, name, mimeType)",
            )
            .execute()
            .get("files", [])
        )

        for doc in documents:
            if db.query(TelemetryEvent).filter(
                TelemetryEvent.source == TelemetrySource.DRIVE_OCR,
                TelemetryEvent.payload["drive_file_id"].astext == doc["id"],
            ).first():
                continue  # already harvested this file

            raw = service.files().get_media(fileId=doc["id"]).execute()
            try:
                ocr_text = ocr_bytes(raw, doc["mimeType"])
            except RuntimeError:
                ocr_text = ""

            signals = extract_financial_signals(ocr_text)
            db.add(
                TelemetryEvent(
                    source=TelemetrySource.DRIVE_OCR,
                    entity_uid=lead_uid,
                    title=f"Financial doc OCR: {doc['name']}",
                    payload={"drive_file_id": doc["id"], "ocr_signals": signals},
                )
            )
            processed += 1

    db.commit()
    return processed
