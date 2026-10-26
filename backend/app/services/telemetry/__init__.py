from app.config import get_settings
from app.services.telemetry.adsb import ADSBAdapter
from app.services.telemetry.apollo import ApolloAdapter
from app.services.telemetry.base import RawTelemetryRecord, TelemetryAdapter
from app.services.telemetry.cme_globex import CMEGlobexAdapter
from app.services.telemetry.cobalt_intelligence import CobaltIntelligenceAdapter
from app.services.telemetry.datalastic import DatalasticAdapter
from app.services.telemetry.fred import FREDAdapter
from app.services.telemetry.gdelt import GDELTAdapter
from app.services.telemetry.gfw_4wings import GFW4WingsAdapter
from app.services.telemetry.highergov import HigherGovAdapter
from app.services.telemetry.import_genius import ImportGeniusAdapter
from app.services.telemetry.noaa_nws import NOAANWSAdapter
from app.services.telemetry.openfda import OpenFDATelemetryAdapter
from app.services.telemetry.planet_labs import PlanetLabsAdapter
from app.services.telemetry.rail_traffic import RailTrafficAdapter
from app.services.telemetry.regrid import RegridAdapter
from app.services.telemetry.seavantage import SeaVantageAdapter
from app.services.telemetry.sonar import SONARAdapter
from app.services.telemetry.sos_registries import SOSRegistriesAdapter
from app.services.telemetry.transmetriq import TransmetriQAdapter
from app.services.telemetry.ucc_filings import UCCFilingsAdapter
from app.services.telemetry.usace import USACEAdapter
from app.services.telemetry.vesselfinder import VesselFinderAdapter
from app.services.telemetry.xeneta import XenetaAdapter


def all_adapters() -> list[TelemetryAdapter]:
    settings = get_settings()
    return [
        CMEGlobexAdapter(api_key=settings.databento_api_key),
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
        USACEAdapter(
            datalastic_api_key=settings.datalastic_api_key,
            vesselfinder_api_key=settings.vesselfinder_api_key,
            cwms_api_key=settings.cwms_api_key,
        ),
        NOAANWSAdapter(),
        FREDAdapter(api_key=settings.fred_api_key),
        RailTrafficAdapter(api_key=settings.rail_traffic_api_key),
        ADSBAdapter(api_key=settings.adsb_api_key),
        TransmetriQAdapter(api_key=settings.transmetriq_api_key),
        SONARAdapter(api_key=settings.sonar_api_key),
        PlanetLabsAdapter(api_key=settings.planet_labs_api_key),
        XenetaAdapter(api_key=settings.xeneta_api_key),
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
    "NOAANWSAdapter",
    "FREDAdapter",
    "RailTrafficAdapter",
    "ADSBAdapter",
    "TransmetriQAdapter",
    "SONARAdapter",
    "PlanetLabsAdapter",
    "XenetaAdapter",
    "all_adapters",
]
