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
    CME_GLOBEX = "cme_globex"
    IMPORT_GENIUS = "import_genius"
    SEAVANTAGE = "seavantage"
    UCC_FILINGS = "ucc_filings"
    SOS_REGISTRIES = "sos_registries"
    REGRID = "regrid"
    DRIVE_OCR = "drive_ocr"
