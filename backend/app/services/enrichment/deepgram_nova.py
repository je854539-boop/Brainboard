"""Deepgram Nova transcription adapter.

Transcribes an already-recorded audio file (e.g. a voicemail, a SignalWire
dialer recording, or a call recording already sitting in a lead's Drive
dossier) via Deepgram's Nova model
(https://api.deepgram.com/v1/listen?model=nova-2). This is a transcription
utility only -- it does not itself place or receive calls; that live path
is services/signalwire_adapter.py + services/dialer.py.
"""

import httpx

from app.models.enums import TelemetrySource
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery

BASE_URL = "https://api.deepgram.com/v1"


class DeepgramNovaAdapter(EnrichmentAdapter):
    source = TelemetrySource.DEEPGRAM_NOVA

    def enrich(self, query: EnrichmentQuery) -> dict:
        if not query.audio_url:
            raise ValueError("Deepgram Nova transcription requires audio_url (a recording already in the dossier)")

        with httpx.Client(
            base_url=BASE_URL, headers={"Authorization": f"Token {self.api_key}"}, timeout=60
        ) as client:
            response = client.post(
                "/listen",
                params={"model": "nova-2", "smart_format": "true"},
                json={"url": query.audio_url},
            )
            response.raise_for_status()
            return response.json()
