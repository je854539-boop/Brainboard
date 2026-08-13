"""Common interface for macro-surveillance telemetry adapters.

Each adapter wraps one upstream data provider (CME Globex, Import Genius,
SeaVantage, UCC filings, SOS corporate registries, Regrid parcel data) and
normalizes its output into TelemetryEvent rows. An adapter with no API key
configured is inert (`enabled` is False, `ingest` is a no-op) rather than
erroring, so the surveillance loop can run with a partial provider roster.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.enums import TelemetrySource
from app.models.orm import TelemetryEvent

logger = logging.getLogger("brainboard.telemetry")


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

    def ingest(self, db: Session) -> dict:
        """Returns {"count": int, "error": str | None}. A failure in one
        adapter (network blocked, bad key, upstream outage) is caught and
        reported here rather than raised, so a batch sweep across multiple
        adapters can't have one flaky source 500 the whole call and lose
        results the other adapters already produced."""
        if not self.enabled:
            return {"count": 0, "error": None}
        try:
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
            return {"count": len(records), "error": None}
        except Exception as exc:  # noqa: BLE001 -- one bad provider must not break the whole sweep
            db.rollback()
            logger.exception("Telemetry adapter %s failed to ingest", self.source.value)
            return {"count": 0, "error": str(exc)}
