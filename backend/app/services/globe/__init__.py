from app.config import get_settings
from app.services.globe.base import GlobeAdapter, RawGlobeSignal
from app.services.globe.gdelt import GDELTAdapter
from app.services.globe.gfw_4wings import GFW4WingsAdapter


def all_globe_adapters() -> list[GlobeAdapter]:
    settings = get_settings()
    return [
        GFW4WingsAdapter(api_key=settings.gfw_api_key),
        GDELTAdapter(api_key=settings.gdelt_api_key),
    ]


__all__ = ["GlobeAdapter", "RawGlobeSignal", "GFW4WingsAdapter", "GDELTAdapter", "all_globe_adapters"]
