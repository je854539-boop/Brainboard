"""Common interface for the 3D globe's geospatial data sources."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.enums import TelemetrySource
from app.models.orm import GlobeSignal


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

    def ingest(self, db: Session) -> int:
        if not self.enabled:
            return 0
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
        return len(signals)
