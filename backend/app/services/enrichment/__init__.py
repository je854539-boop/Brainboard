from app.config import get_settings
from app.services.enrichment.apollo import ApolloAdapter
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery
from app.services.enrichment.cme_globex import CMEGlobexEnrichmentAdapter
from app.services.enrichment.cobalt_intelligence import CobaltIntelligenceAdapter
from app.services.enrichment.deepgram_nova import DeepgramNovaAdapter
from app.services.enrichment.gdelt import GDELTEnrichmentAdapter
from app.services.enrichment.gfw_4wings import GFW4WingsEnrichmentAdapter
from app.services.enrichment.highergov import HigherGovEnrichmentAdapter
from app.services.enrichment.import_genius import ImportGeniusEnrichmentAdapter
from app.services.enrichment.interzoid import InterzoidAdapter
from app.services.enrichment.openfda import OpenFDAAdapter
from app.services.enrichment.regrid import RegridEnrichmentAdapter
from app.services.enrichment.seavantage import SeaVantageEnrichmentAdapter


def all_enrichment_adapters() -> list[EnrichmentAdapter]:
    """Every provider the dialer team wants run against a lead at intake,
    to surface info the call didn't cover. Each reuses the same API key
    setting as its telemetry/globe counterpart -- one credential per
    vendor, used both for macro sweeps and targeted per-lead lookups."""
    settings = get_settings()
    return [
        CobaltIntelligenceAdapter(api_key=settings.cobalt_intelligence_api_key),
        InterzoidAdapter(api_key=settings.interzoid_api_key),
        ApolloAdapter(api_key=settings.apollo_api_key),
        OpenFDAAdapter(api_key=settings.openfda_api_key),
        DeepgramNovaAdapter(api_key=settings.deepgram_api_key),
        CMEGlobexEnrichmentAdapter(api_key=settings.cme_globex_api_key),
        ImportGeniusEnrichmentAdapter(api_key=settings.import_genius_api_key),
        SeaVantageEnrichmentAdapter(api_key=settings.seavantage_api_key),
        RegridEnrichmentAdapter(api_key=settings.regrid_api_key),
        HigherGovEnrichmentAdapter(api_key=settings.highergov_api_key),
        GFW4WingsEnrichmentAdapter(api_key=settings.gfw_api_key),
        GDELTEnrichmentAdapter(api_key=settings.gdelt_api_key),
    ]


__all__ = [
    "EnrichmentAdapter",
    "EnrichmentQuery",
    "CobaltIntelligenceAdapter",
    "InterzoidAdapter",
    "ApolloAdapter",
    "OpenFDAAdapter",
    "DeepgramNovaAdapter",
    "CMEGlobexEnrichmentAdapter",
    "ImportGeniusEnrichmentAdapter",
    "SeaVantageEnrichmentAdapter",
    "RegridEnrichmentAdapter",
    "HigherGovEnrichmentAdapter",
    "GFW4WingsEnrichmentAdapter",
    "GDELTEnrichmentAdapter",
    "all_enrichment_adapters",
]
