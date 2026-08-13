"""Post-call analysis via Deepgram Nova.

Transcribes, diarizes, and scores sentiment on an already-recorded call --
either an uploaded audio file or a URL to one already hosted (e.g. in a
Drive dossier). This analyzes recordings after the fact; it does not
place, receive, or route calls, and there is no telephony integration
anywhere in Brainboard.
"""

from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import ActivityEventType, ActivitySource, CallAnalysisStatus
from app.models.orm import CallRecording
from app.services import pipeline

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_PARAMS = {
    "model": "nova-2",
    "smart_format": "true",
    "punctuate": "true",
    "diarize": "true",
    "sentiment": "true",
    "summarize": "v2",
}


def _parse_deepgram_response(payload: dict) -> dict:
    results = payload.get("results", {})
    channels = results.get("channels", [])
    transcript = ""
    if channels:
        alternatives = channels[0].get("alternatives", [])
        if alternatives:
            transcript = alternatives[0].get("transcript", "")

    summary_block = results.get("summary")
    summary = summary_block.get("short") if isinstance(summary_block, dict) else None

    sentiment_block = results.get("sentiments")
    sentiment = sentiment_block if isinstance(sentiment_block, dict) else None

    speakers = results.get("utterances")

    return {
        "transcript": transcript,
        "summary": summary,
        "sentiment": sentiment,
        "speakers": speakers,
        "duration_seconds": payload.get("metadata", {}).get("duration"),
    }


def analyze_call(
    db: Session,
    *,
    source_label: str,
    lead_uid=None,
    audio_bytes: bytes | None = None,
    audio_content_type: str | None = None,
    audio_url: str | None = None,
) -> CallRecording:
    settings = get_settings()

    record = CallRecording(
        lead_uid=lead_uid,
        source_label=source_label,
        audio_url=audio_url,
        status=CallAnalysisStatus.PENDING,
    )
    db.add(record)
    db.flush()

    if not settings.deepgram_api_key:
        record.status = CallAnalysisStatus.FAILED
        record.error = "deepgram_nova is not configured (no API key)"
        db.commit()
        db.refresh(record)
        return record

    if audio_bytes is None and not audio_url:
        record.status = CallAnalysisStatus.FAILED
        record.error = "either an uploaded file or an audio_url is required"
        db.commit()
        db.refresh(record)
        return record

    try:
        headers = {"Authorization": f"Token {settings.deepgram_api_key}"}
        with httpx.Client(timeout=120) as client:
            if audio_bytes is not None:
                headers["Content-Type"] = audio_content_type or "audio/wav"
                response = client.post(DEEPGRAM_URL, params=DEEPGRAM_PARAMS, headers=headers, content=audio_bytes)
            else:
                headers["Content-Type"] = "application/json"
                response = client.post(DEEPGRAM_URL, params=DEEPGRAM_PARAMS, headers=headers, json={"url": audio_url})
            response.raise_for_status()
            parsed = _parse_deepgram_response(response.json())

        record.transcript = parsed["transcript"]
        record.summary = parsed["summary"]
        record.sentiment = parsed["sentiment"]
        record.speakers = parsed["speakers"]
        record.duration_seconds = parsed["duration_seconds"]
        record.status = CallAnalysisStatus.COMPLETED
        record.completed_at = datetime.now(timezone.utc)

        if lead_uid is not None:
            pipeline.log_activity(
                db, lead_uid, ActivityEventType.CALL_ANALYSIS, ActivitySource.SYSTEM,
                field_name="call_analysis", new_value=source_label,
            )
    except Exception as exc:  # noqa: BLE001 -- report the failure on the record, don't 500 the upload
        record.status = CallAnalysisStatus.FAILED
        record.error = str(exc)

    db.commit()
    db.refresh(record)
    return record
