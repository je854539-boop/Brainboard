from app.config import get_settings
from app.services.globe.base import GlobeAdapter, RawGlobeSignal
from app.services.globe.gfw_4wings import GFW4WingsAdapter
from app.services.globe.gled import GLEDAdapter


def all_globe_adapters() -> list[GlobeAdapter]:
    settings = get_settings()
    return [
        GFW4WingsAdapter(api_key=settings.gfw_api_key),
        GLEDAdapter(api_key=settings.gled_api_key),
    ]


__all__ = ["GlobeAdapter", "RawGlobeSignal", "GFW4WingsAdapter", "GLEDAdapter", "all_globe_adapters"]
