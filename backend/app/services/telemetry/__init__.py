from app.config import get_settings
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter
from app.services.telemetry.cme_globex import CMEGlobexAdapter
from app.services.telemetry.highergov import HigherGovAdapter
from app.services.telemetry.import_genius import ImportGeniusAdapter
from app.services.telemetry.openfda import OpenFDATelemetryAdapter
from app.services.telemetry.regrid import RegridAdapter
from app.services.telemetry.seavantage import SeaVantageAdapter
from app.services.telemetry.sos_registries import SOSRegistriesAdapter
from app.services.telemetry.ucc_filings import UCCFilingsAdapter


def all_adapters() -> list[TelemetryAdapter]:
    settings = get_settings()
    return [
        CMEGlobexAdapter(api_key=settings.cme_globex_api_key),
        ImportGeniusAdapter(api_key=settings.import_genius_api_key),
        SeaVantageAdapter(api_key=settings.seavantage_api_key),
        UCCFilingsAdapter(),
        SOSRegistriesAdapter(),
        RegridAdapter(api_key=settings.regrid_api_key),
        HigherGovAdapter(api_key=settings.highergov_api_key),
        OpenFDATelemetryAdapter(api_key=settings.openfda_api_key),
    ]


__all__ = [
    "RawTelemetryRecord",
    "TelemetryAdapter",
    "CMEGlobexAdapter",
    "ImportGeniusAdapter",
    "SeaVantageAdapter",
    "UCCFilingsAdapter",
    "SOSRegistriesAdapter",
    "RegridAdapter",
    "HigherGovAdapter",
    "OpenFDATelemetryAdapter",
    "all_adapters",
]
