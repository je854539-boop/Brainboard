import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.orm import CallRecording
from app.schemas import CallFromUrlRequest, CallRecordingOut
from app.services import call_analysis

router = APIRouter(prefix="/api/calls", tags=["calls"])


@router.get("", response_model=list[CallRecordingOut])
def list_calls(lead_uid: uuid.UUID | None = None, limit: int = 50, db: Session = Depends(get_db)):
    query = select(CallRecording)
    if lead_uid:
        query = query.where(CallRecording.lead_uid == lead_uid)
    query = query.order_by(CallRecording.created_at.desc()).limit(min(limit, 200))
    return db.execute(query).scalars().all()


@router.get("/{call_id}", response_model=CallRecordingOut)
def get_call(call_id: uuid.UUID, db: Session = Depends(get_db)):
    record = db.get(CallRecording, call_id)
    if record is None:
        raise HTTPException(status_code=404, detail="call recording not found")
    return record


@router.post("/upload", response_model=CallRecordingOut, status_code=201)
async def upload_call(
    file: UploadFile,
    lead_uid: uuid.UUID | None = Form(default=None),
    db: Session = Depends(get_db),
):
    audio_bytes = await file.read()
    return call_analysis.analyze_call(
        db,
        source_label=file.filename or "uploaded-call",
        lead_uid=lead_uid,
        audio_bytes=audio_bytes,
        audio_content_type=file.content_type,
    )


@router.post("/from-url", response_model=CallRecordingOut, status_code=201)
def analyze_call_url(payload: CallFromUrlRequest, db: Session = Depends(get_db)):
    return call_analysis.analyze_call(
        db,
        source_label=payload.source_label or payload.audio_url,
        lead_uid=payload.lead_uid,
        audio_url=payload.audio_url,
    )
