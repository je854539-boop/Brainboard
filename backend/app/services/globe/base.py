"""Common interface for the 3D globe's geospatial data sources."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.enums import TelemetrySource
from app.models.orm import GlobeSignal

logger = logging.getLogger("brainboard.globe")


@dataclass
class RawGlobeSignal:
    latitude: float
    longitude: float
    title: str
    intensity: float | None = None
    payload: dict = field(default_factory=dict)
    entity_uid: str | None = None


class GlobeAdapter(ABC):
    source: TelemetrySource

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @abstractmethod
    def fetch_signals(self) -> list[RawGlobeSignal]:
        raise NotImplementedError

    def ingest(self, db: Session) -> dict:
        """Returns {"count": int, "error": str | None}. A failure in one
        adapter (network blocked, bad key, upstream outage) is caught and
        reported here rather than raised, so a batch refresh across
        multiple adapters can't have one flaky source 500 the whole call
        and lose results the other adapters already produced."""
        if not self.enabled:
            return {"count": 0, "error": None}
        try:
            signals = self.fetch_signals()
            for signal in signals:
                db.add(
                    GlobeSignal(
                        source=self.source,
                        latitude=signal.latitude,
                        longitude=signal.longitude,
                        intensity=signal.intensity,
                        title=signal.title,
                        payload=signal.payload,
                        entity_uid=signal.entity_uid,
                    )
                )
            db.commit()
            return {"count": len(signals), "error": None}
        except Exception as exc:  # noqa: BLE001 -- one bad provider must not break the whole refresh
            db.rollback()
            logger.exception("Globe adapter %s failed to ingest", self.source.value)
            return {"count": 0, "error": str(exc)}
