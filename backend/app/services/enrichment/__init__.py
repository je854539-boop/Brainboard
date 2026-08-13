from app.config import get_settings
from app.services.enrichment.apollo import ApolloAdapter
from app.services.enrichment.base import EnrichmentAdapter, EnrichmentQuery
from app.services.enrichment.cobalt_intelligence import CobaltIntelligenceAdapter
from app.services.enrichment.deepgram_nova import DeepgramNovaAdapter
from app.services.enrichment.interzoid import InterzoidAdapter
from app.services.enrichment.openfda import OpenFDAAdapter


def all_enrichment_adapters() -> list[EnrichmentAdapter]:
    settings = get_settings()
    return [
        CobaltIntelligenceAdapter(api_key=settings.cobalt_intelligence_api_key),
        InterzoidAdapter(api_key=settings.interzoid_api_key),
        ApolloAdapter(api_key=settings.apollo_api_key),
        OpenFDAAdapter(api_key=settings.openfda_api_key),
        DeepgramNovaAdapter(api_key=settings.deepgram_api_key),
    ]


__all__ = [
    "EnrichmentAdapter",
    "EnrichmentQuery",
    "CobaltIntelligenceAdapter",
    "InterzoidAdapter",
    "ApolloAdapter",
    "OpenFDAAdapter",
    "DeepgramNovaAdapter",
    "all_enrichment_adapters",
]
