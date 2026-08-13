from app.config import get_settings
from app.services.globe.base import GlobeAdapter, RawGlobeSignal
from app.services.globe.datalastic import DatalasticAdapter
from app.services.globe.gdelt import GDELTAdapter
from app.services.globe.gfw_4wings import GFW4WingsAdapter
from app.services.globe.vesselfinder import VesselFinderAdapter


def all_globe_adapters() -> list[GlobeAdapter]:
    settings = get_settings()
    return [
        GFW4WingsAdapter(api_key=settings.gfw_api_key),
        GDELTAdapter(api_key=settings.gdelt_api_key),
        DatalasticAdapter(api_key=settings.datalastic_api_key),
        VesselFinderAdapter(api_key=settings.vesselfinder_api_key),
    ]


__all__ = [
    "GlobeAdapter",
    "RawGlobeSignal",
    "GFW4WingsAdapter",
    "GDELTAdapter",
    "DatalasticAdapter",
    "VesselFinderAdapter",
    "all_globe_adapters",
]
