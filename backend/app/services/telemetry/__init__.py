from app.config import get_settings
from app.services.telemetry.apollo import ApolloAdapter
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter
from app.services.telemetry.cme_globex import CMEGlobexAdapter
from app.services.telemetry.cobalt_intelligence import CobaltIntelligenceAdapter
from app.services.telemetry.datalastic import DatalasticAdapter
from app.services.telemetry.gdelt import GDELTAdapter
from app.services.telemetry.gfw_4wings import GFW4WingsAdapter
from app.services.telemetry.highergov import HigherGovAdapter
from app.services.telemetry.import_genius import ImportGeniusAdapter
from app.services.telemetry.openfda import OpenFDATelemetryAdapter
from app.services.telemetry.regrid import RegridAdapter
from app.services.telemetry.seavantage import SeaVantageAdapter
from app.services.telemetry.sos_registries import SOSRegistriesAdapter
from app.services.telemetry.ucc_filings import UCCFilingsAdapter
from app.services.telemetry.usace import USACEAdapter
from app.services.telemetry.vesselfinder import VesselFinderAdapter


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
        DatalasticAdapter(api_key=settings.datalastic_api_key),
        VesselFinderAdapter(api_key=settings.vesselfinder_api_key),
        GDELTAdapter(api_key=settings.gdelt_api_key),
        GFW4WingsAdapter(api_key=settings.gfw_api_key),
        CobaltIntelligenceAdapter(api_key=settings.cobalt_intelligence_api_key),
        ApolloAdapter(api_key=settings.apollo_api_key),
        USACEAdapter(api_key=settings.usace_api_key),
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
    "DatalasticAdapter",
    "VesselFinderAdapter",
    "GDELTAdapter",
    "GFW4WingsAdapter",
    "CobaltIntelligenceAdapter",
    "ApolloAdapter",
    "USACEAdapter",
    "all_adapters",
]
