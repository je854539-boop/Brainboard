"""Common interface for targeted, per-lead enrichment adapters -- as
opposed to app/services/telemetry's passive macro-silo sweeps, these are
called with a specific lead's identifying details and return one result
for that lead. An adapter with no API key configured is inert."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.enums import TelemetrySource
from app.models.orm import EnrichmentResult


@dataclass
class EnrichmentQuery:
    entity_uid: str
    business_name: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    state: str | None = None
    address: str | None = None
    audio_url: str | None = None  # for Deepgram Nova: a recording already stored in the dossier, not live telephony
    extra: dict = field(default_factory=dict)


class EnrichmentAdapter(ABC):
    source: TelemetrySource

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @abstractmethod
    def enrich(self, query: EnrichmentQuery) -> dict:
        """Look up this one entity against the upstream provider and
        return a JSON-serializable payload. Implementations must be
        written against that provider's actual API contract per its
        vendor docs before going live."""
        raise NotImplementedError

    def run(self, db: Session, query: EnrichmentQuery) -> EnrichmentResult:
        result = EnrichmentResult(entity_uid=query.entity_uid, source=self.source, payload={})
        if not self.enabled:
            result.error = f"{self.source.value} is not configured (no API key)"
            db.add(result)
            return result
        try:
            result.payload = self.enrich(query)
        except Exception as exc:  # noqa: BLE001 -- one bad provider must not break the enrichment batch
            result.error = str(exc)
        db.add(result)
        return result
