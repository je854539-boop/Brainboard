from enum import Enum


class CoBroker(str, Enum):
    """The 18 validated co-brokers. Values are canonical and must match
    Master Log V2 column values string-for-string."""

    NICK_F = "Nick F"
    MIKE_F = "Mike F"
    VINNY = "Vinny"
    VICTOR = "Victor"
    GALLO = "Gallo"
    SHAUN = "Shaun"
    KRIS = "Kris"
    DAN_STOL = "DanStol"
    ROMAN = "Roman"
    JAMES = "James"
    ALFRED = "Alfred"
    MARCUS = "Marcus"
    RICKY = "Ricky"
    ZACK = "Zack"
    EMILIO = "Emilio"
    JORGE = "Jorge"
    TONY = "Tony"
    SEB = "Seb"


class MasterLogStatus(str, Enum):
    """The 13 validated pipeline statuses. Values are canonical and must
    match Master Log V2 column values string-for-string."""

    NEW_LEAD = "New lead"
    APP_SENT = "App Sent"
    DOCS_OWED = "Docs Owed"
    CHASE_DOCS = "Chase Docs"
    DOCS_IN = "Docs in"
    IN_NEGOTIATION = "In negotiation"
    OFFER_MADE_NOT_SOLD = "Offer Made Not Sold"
    SOLD_DEAL_KILLED = "Sold Deal Killed"
    DEAL_STALLED_PROXY_PASS = "Deal Stalled Proxy Pass"
    FUNDED = "Funded"
    GHOSTED = "Ghosted"
    LOSS_TO_COMPETITOR = "Loss to Competitor"
    DOG_SHIT = "Dog Shit"


# Statuses that end a lead's lifecycle. FUNDED is the single "success" event
# for Kaplan-Meier survival analysis; the rest are attrition/"death" events.
# Everything not listed here is treated as active (right-censored) for KM.
TERMINAL_SUCCESS_STATUSES: frozenset[MasterLogStatus] = frozenset({MasterLogStatus.FUNDED})
TERMINAL_ATTRITION_STATUSES: frozenset[MasterLogStatus] = frozenset(
    {
        MasterLogStatus.GHOSTED,
        MasterLogStatus.LOSS_TO_COMPETITOR,
        MasterLogStatus.DOG_SHIT,
        MasterLogStatus.SOLD_DEAL_KILLED,
        MasterLogStatus.DEAL_STALLED_PROXY_PASS,
    }
)
TERMINAL_STATUSES: frozenset[MasterLogStatus] = TERMINAL_SUCCESS_STATUSES | TERMINAL_ATTRITION_STATUSES


class SiloName(str, Enum):
    """The 9 macro-surveillance silo tabs."""

    CME_MACRO_FUNNEL = "CME Macro Funnel"
    TARIFF_SILO = "Tariff Silo"
    FOOD_PROCESSING_FABRICATION_SILO = "Food Processing & Fabrication Silo"
    OIL_GAS_REFINING_SILO = "Oil & Gas / Refining Silo"
    HEAVY_MACHINERY_INDUSTRIAL_EQUIPMENT_SILO = "Heavy Machinery & Industrial Equipment Silo"
    AGRICULTURE_GRAIN_HANDLING_SILO = "Agriculture & Grain Handling Silo"
    HEALTHCARE_PHARMA_SILO = "Healthcare & Pharma Silo"
    ECOMMERCE_FULFILLMENT_SILO = "E-Commerce & Fulfillment Silo"
    HIGHERGOV_FUNNEL = "HigherGov Funnel"


class SiloCandidateStatus(str, Enum):
    PENDING = "pending"
    CONVERTED = "converted"
    DISMISSED = "dismissed"


class TelemetrySource(str, Enum):
    """Every external data provider, used both for passive macro-silo
    telemetry sweeps (TelemetryEvent) and targeted per-lead lookups
    (EnrichmentResult) -- the vendor is the same either way, only the
    query mode differs."""

    CME_GLOBEX = "cme_globex"
    IMPORT_GENIUS = "import_genius"
    SEAVANTAGE = "seavantage"
    UCC_FILINGS = "ucc_filings"
    SOS_REGISTRIES = "sos_registries"
    REGRID = "regrid"
    DRIVE_OCR = "drive_ocr"
    COBALT_INTELLIGENCE = "cobalt_intelligence"
    INTERZOID = "interzoid"
    APOLLO = "apollo"
    OPENFDA = "openfda"
    DEEPGRAM_NOVA = "deepgram_nova"
    HIGHERGOV = "highergov"
    GFW_4WINGS = "gfw_4wings"
    GDELT = "gdelt"


# Sources meant for targeted, per-lead enrichment lookups (uploaded leads /
# leads sourced off the dialer) -- kept in sync with
# services/enrichment/__init__.py::all_enrichment_adapters().
ENRICHMENT_SOURCES: frozenset[TelemetrySource] = frozenset(
    {
        TelemetrySource.COBALT_INTELLIGENCE,
        TelemetrySource.INTERZOID,
        TelemetrySource.APOLLO,
        TelemetrySource.OPENFDA,
        TelemetrySource.DEEPGRAM_NOVA,
        TelemetrySource.CME_GLOBEX,
        TelemetrySource.IMPORT_GENIUS,
        TelemetrySource.SEAVANTAGE,
        TelemetrySource.REGRID,
        TelemetrySource.HIGHERGOV,
        TelemetrySource.GFW_4WINGS,
        TelemetrySource.GDELT,
    }
)

# Sources meant for passive macro-silo surveillance sweeps -- kept in sync
# with services/telemetry/__init__.py::all_adapters().
MACRO_TELEMETRY_SOURCES: frozenset[TelemetrySource] = frozenset(
    {
        TelemetrySource.CME_GLOBEX,
        TelemetrySource.IMPORT_GENIUS,
        TelemetrySource.SEAVANTAGE,
        TelemetrySource.UCC_FILINGS,
        TelemetrySource.SOS_REGISTRIES,
        TelemetrySource.REGRID,
        TelemetrySource.HIGHERGOV,
        TelemetrySource.OPENFDA,
    }
)

# Geospatial sources plotted on the 3D globe.
GLOBE_SOURCES: frozenset[TelemetrySource] = frozenset({TelemetrySource.GFW_4WINGS, TelemetrySource.GDELT})


class ActivityEventType(str, Enum):
    """Every lead-lifecycle event the hazard engine can eventually treat
    as a covariate -- "every click, every calendar change, every note
    change" per spec."""

    CREATED = "created"
    NOTE_CHANGE = "note_change"
    FOLLOW_UP_CHANGE = "follow_up_change"
    STATUS_CHANGE = "status_change"
    FIELD_CHANGE = "field_change"
    CALENDAR_SYNC = "calendar_sync"
    CLICK = "click"
    ENRICHMENT = "enrichment"
    SHEET_SYNC = "sheet_sync"
    CALL_ANALYSIS = "call_analysis"


class ActivitySource(str, Enum):
    UI = "ui"
    API = "api"
    WEBHOOK_SHEET = "webhook_sheet"
    SYSTEM = "system"


class CallAnalysisStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class BrainMode(str, Enum):
    SHADOW = "shadow"
    LIVE = "live"
