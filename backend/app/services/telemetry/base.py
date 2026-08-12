"""Common interface for macro-surveillance telemetry adapters.

Each adapter wraps one upstream data provider (CME Globex, Import Genius,
SeaVantage, UCC filings, SOS corporate registries, Regrid parcel data) and
normalizes its output into TelemetryEvent rows. An adapter with no API key
configured is inert (`enabled` is False, `ingest` is a no-op) rather than
erroring, so the surveillance loop can run with a partial provider roster.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.enums import TelemetrySource
from app.models.orm import TelemetryEvent


@dataclass
class RawTelemetryRecord:
    title: str
    payload: dict
    entity_uid: str | None = None  # links the event to a lead_uid / candidate_uid when known


class TelemetryAdapter(ABC):
    source: TelemetrySource

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @abstractmethod
    def fetch(self) -> list[RawTelemetryRecord]:
        """Pull the latest raw records from the upstream provider.

        Implementations must be written against that provider's actual API
        contract (auth scheme, pagination, rate limits) per its vendor
        documentation before going live -- see the module docstring on each
        adapter for what is stubbed vs. wired."""
        raise NotImplementedError

    def ingest(self, db: Session) -> int:
        if not self.enabled:
            return 0
        records = self.fetch()
        for record in records:
            db.add(
                TelemetryEvent(
                    source=self.source,
                    entity_uid=record.entity_uid,
                    title=record.title,
                    payload=record.payload,
                )
            )
        db.commit()
        return len(records)
