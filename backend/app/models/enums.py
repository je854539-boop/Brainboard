from enum import Enum


class CoBroker(str, Enum):
    """The 20 validated co-brokers. Values are canonical and must match
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
    BOULA = "Boula"
    ARIEL = "Ariel"
    TONY = "Tony"
    MAX = "Max"
    SEBASTIAN = "Sebastian"


class MasterLogStatus(str, Enum):
    """The 13 validated pipeline statuses. Values are canonical and must
    match Master Log V2 column values string-for-string."""

    NEW_LEAD = "New lead"
    APP_SENT = "App Sent"
    DOCS_OWED = "Docs Being Chased"
    CHASE_DOCS = "Chase Docs"
    DOCS_IN = "Docs in"
    IN_NEGOTIATION = "In Negotiation"
    OFFER_MADE_NOT_SOLD = "Offer Made Not Sold"
    SOLD_DEAL_KILLED = "Sold Deal Killed"
    DEAL_STALLED_PROXY_PASS = "Deal Stalled Proxy Pass"
    FUNDED = "Funded"
    GHOSTED = "Ghosted"
    LOSS_TO_COMPETITOR = "Lost To Competitor"
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
    # A one-time bulk UCC-lien batch the user already has (name, phone,
    # email, revenue range already attached per record -- a purchased
    # enriched UCC-lead product, not a bare debtor filing), distinct from
    # the other 8 silos' live telemetry sweeps. Lives in its own silo tab
    # on its own spreadsheet (settings.google_ucc_batch_sheet_id), not
    # Master Log V2's 9-silo workbook, and feeds its own shadow-mode
    # population -- see brain.py::ucc_batch_mode. See services/ucc_batch.py.
    UCC_BATCH_SILO = "UCC Batch Silo"
    # A second, distinct purchased batch -- 18,000 contacts, current/
    # prior MCA position + $100K/mo+ revenue, a different list and a
    # different real signal (proven MCA payment behavior, not a UCC debt
    # filing) from the UCC Batch Silo above. Kept as its own silo rather
    # than merged into UCC_BATCH_SILO so conversion performance between
    # the two purchased sources stays separately attributable. See
    # PURCHASED_BATCH_SILOS below and services/mca_history_batch.py.
    MCA_HISTORY_BATCH_SILO = "MCA History Batch Silo"


# Every silo whose candidates are staged (SiloCandidate, not counted
# toward brain.py's lead threshold) until a real human touchpoint
# promotes one into Master Log V2 -- see brain.py's current_mode /
# per-silo *_mode docstrings for why this split exists. Add a new
# purchased-batch silo here (and nowhere else) to get the same
# separate-shadow-population treatment automatically.
PURCHASED_BATCH_SILOS: frozenset[SiloName] = frozenset({SiloName.UCC_BATCH_SILO, SiloName.MCA_HISTORY_BATCH_SILO})


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
    DATALASTIC = "datalastic"
    VESSELFINDER = "vesselfinder"
    USACE = "usace"
    # Free/keyless, real, well-documented government API (api.weather.gov)
    # -- structured hurricane/tornado/flood/winter-storm alerts by exact
    # zone, issued the moment NOAA declares them. A better primary source
    # for disaster detection than waiting on GDELT news coverage to catch
    # up, since it doesn't depend on a journalist writing about it first.
    NOAA_NWS = "noaa_nws"
    # Federal Reserve Economic Data (FRED) -- free with a signup key.
    # Regional credit-tightening conditions as a macro context signal:
    # tighter conventional lending in an area means more businesses there
    # are likely to need alternative capital, independent of any
    # commodity or disaster signal. See silo_leadgen.py::
    # _fred_credit_conditions_signals.
    FRED = "fred"
    # HONESTY NOTE: AAR (Association of American Railroads) and the STB
    # (Surface Transportation Board) publish real rail traffic/service-
    # performance data, but as downloadable reports, not a confirmed
    # clean REST API the way CWMS or api.weather.gov are. This source
    # exists so the adapter/signal-classification scaffolding is in
    # place, but telemetry/rail_traffic.py's fetch() is a placeholder
    # until a real access method (API, scheduled file pull, etc.) is
    # confirmed -- same posture as the LPMS note in river_surveillance.py.
    # No Railinc-equivalent car-level tracking exists here; this is
    # macro volume/dwell only, never company-specific.
    RAIL_TRAFFIC = "rail_traffic"
    # ADS-B (Automatic Dependent Surveillance-Broadcast) -- the real
    # AIS-equivalent for aircraft: most cargo aircraft broadcast position
    # openly, unlike railcars or trucks. telemetry/adsb.py is written
    # against a plausible provider shape (ADS-B Exchange / FlightAware)
    # but, like RAIL_TRAFFIC, is unconfirmed against live docs in this
    # sandbox -- see that module's own docstring.
    ADSB = "adsb"
    # TransmetriQ (Railinc's modernized API layer over the same
    # infrastructure RAIL_TRAFFIC's AAR/STB macro-only placeholder sits
    # outside of) -- real, confirmed product: VIN-level railcar tracking
    # across 600+ North American railroads, quote-based pricing, no
    # Railinc shipper-of-record membership required the way raw Umler/
    # EDI access does. This is the real car-level answer RAIL_TRAFFIC
    # explicitly said didn't exist -- see telemetry/transmetriq.py for
    # what's still unconfirmed (exact live endpoint contract).
    TRANSMETRIQ = "transmetriq"
    # FreightWaves SONAR -- real trucking tender-rejection/capacity macro
    # signal (confirmed self-serve tier ~$25-35/mo, full API access
    # quote-based/enterprise). Same macro-context-only posture as FRED:
    # no company-level targeting exists for trucking the way AIS/ADS-B
    # give it for vessels/aircraft.
    SONAR = "sonar"
    # Planet Labs -- real, confirmed pricing ($2,700-$9,650/yr by
    # coverage area on their Monitoring plan). Daily-revisit optical
    # imagery, NOT radar -- this is the land-use/parcel change-detection
    # tool (pairs with Regrid), not the cloud-penetrating disaster tool
    # (that's Sentinel-1 SAR / ICEYE / Capella, a different use case).
    PLANET_LABS = "planet_labs"
    # Xeneta -- real, confirmed air-freight rate/capacity intelligence
    # (30,000+ airport pairs, daily shipper rates, subscription/
    # enterprise pricing, quote-based). The missing piece alongside
    # ADSB: ADS-B gives aircraft POSITION (the AIS-equivalent); Xeneta
    # gives RATE/CAPACITY tightness per lane (the SONAR-equivalent for
    # air). Unlike SONAR/FRED, Xeneta's lane structure is specific
    # enough to carry a real destination location, not just nationwide
    # context -- see silo_leadgen.py::_xeneta_air_freight_signals.
    # WorldACD is a real comparable alternative, not built here.
    XENETA = "xeneta"
    # Sentinel Hub -- commercial-friendly processing API over the same
    # free, open ESA Copernicus Sentinel-1 SAR data discussed at length
    # earlier (radar, sees through cloud cover a real storm brings,
    # unlike Planet Labs' optical sensors). Serves two consumers: a
    # disaster-damage-extent score in silo_leadgen.py (confirming a NOAA
    # alert actually did real damage before gating open the disaster-
    # recovery-construction hunt), and a river-barge presence check in
    # river_surveillance.py, for the real AIS coverage gap on inland
    # barges/towboats discussed earlier this session. One adapter, two
    # genuinely different use cases, same data source.
    SENTINEL_HUB = "sentinel_hub"
    # FMCSA Company Snapshot / QCMobile -- confirmed free (login.gov +
    # WebKey query param), searchable by Carrier Name, DOT#, or MC# (the
    # live Carrier Search screen confirmed name-search, not just DOT/MC
    # as first assumed). Real power-units/driver-count fleet-size
    # verification -- the actual mechanism behind the 25+ truck question
    # for any logistics-flavored lead, including the UCC Batch Silo's
    # trucking-keyword subset. See services/enrichment/fmcsa.py.
    FMCSA = "fmcsa"
    # A one-time bulk ingest of a UCC-lien batch the user already has
    # (not a live per-state pull -- that's TelemetrySource.UCC_FILINGS,
    # still inert/STATE_ENDPOINTS={}). Tags SiloCandidate.origin_source
    # for every row created by services/ucc_batch.py::import_ucc_batch_csv.
    # Deliberately not in ENRICHMENT_SOURCES or MACRO_TELEMETRY_SOURCES
    # below -- it's a one-time import, not a per-lead lookup or a
    # recurring sweep -- same "tag, not a swept/queried source" posture
    # as DRIVE_OCR above.
    UCC_BATCH_UPLOAD = "ucc_batch_upload"
    # Tag for SiloCandidate rows from the MCA History Batch Silo's CSV
    # import (18K contacts, current/prior MCA position + $100K/mo+
    # revenue) -- same "one-time import, not a swept/queried source"
    # posture as UCC_BATCH_UPLOAD above, deliberately absent from
    # ENRICHMENT_SOURCES/MACRO_TELEMETRY_SOURCES for the same reason.
    MCA_HISTORY_BATCH_UPLOAD = "mca_history_batch_upload"


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
        TelemetrySource.DATALASTIC,
        TelemetrySource.VESSELFINDER,
        TelemetrySource.FMCSA,
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
        TelemetrySource.DATALASTIC,
        TelemetrySource.VESSELFINDER,
        TelemetrySource.GDELT,
        TelemetrySource.GFW_4WINGS,
        TelemetrySource.COBALT_INTELLIGENCE,
        TelemetrySource.APOLLO,
        TelemetrySource.USACE,
        TelemetrySource.NOAA_NWS,
        TelemetrySource.FRED,
        TelemetrySource.RAIL_TRAFFIC,
        TelemetrySource.ADSB,
        TelemetrySource.TRANSMETRIQ,
        TelemetrySource.SONAR,
        TelemetrySource.PLANET_LABS,
        TelemetrySource.XENETA,
        TelemetrySource.SENTINEL_HUB,
    }
)

# Geospatial sources plotted on the 3D globe.
GLOBE_SOURCES: frozenset[TelemetrySource] = frozenset(
    {
        TelemetrySource.GFW_4WINGS,
        TelemetrySource.GDELT,
        TelemetrySource.DATALASTIC,
        TelemetrySource.VESSELFINDER,
    }
)


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
    WEBHOOK_CALENDAR = "webhook_calendar"
    SYSTEM = "system"


class CallAnalysisStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class BrainMode(str, Enum):
    SHADOW = "shadow"
    LIVE = "live"


class DatePrecision(str, Enum):
    """How precisely a date field is actually known. funded_date in
    particular is routinely backfilled from memory for deals that funded
    before this was tracked -- MONTH means the day was unknown and the
    stored date is the 1st of that month as a placeholder, not a real
    funding day. Every consumer of funded_date must branch on this before
    treating the day-of-month as meaningful."""

    EXACT = "exact"
    MONTH = "month"


class GeofenceEventType(str, Enum):
    """A single vessel observation classified against a monitored waterway
    zone -- see services/river_surveillance.py::WATERWAY_ZONES."""

    ZONE_ENTRY = "zone_entry"
    ZONE_EXIT = "zone_exit"
    VELOCITY_ANOMALY = "velocity_anomaly"  # stationary/near-0kt beyond the idle threshold inside a restricted zone
    LOCK_QUEUE_DELAY = "lock_queue_delay"  # AIS-proxy: idle vessel cluster at a lock, see telemetry/usace.py
    LOCK_CLOSURE = "lock_closure"  # CWMS gate-change activity crossing the disruption threshold


class WaterwayTriggerType(str, Enum):
    """The outputs of river_surveillance.py's trigger matrix."""

    DISTRESS_SUPPLY_STARVATION = "distress_supply_starvation"
    EXPANSION_THROUGHPUT_SPIKE = "expansion_throughput_spike"
    # A company's cargo resolved at a coastal zone (Port Arthur, Corpus
    # Christi) and again at a river zone (a Mississippi/Illinois/Ohio
    # lock) within the transload window -- real evidence the same
    # shipment moved off an ocean vessel onto a barge and continued
    # inland, not two unrelated sightings. Vessel identity (MMSI) is
    # NOT what links these two legs -- a transload swaps the vessel
    # entirely -- so this is keyed on the resolved company name from
    # cargo cross-reference, the same way every other entity match in
    # this file works.
    INLAND_TRANSLOAD_CONFIRMED = "inland_transload_confirmed"


class DialerCampaignType(str, Enum):
    """SignalWire dialer campaign types (services/dialer.py). Phase 1 only
    implements OUTBOUND (queue-building + call placement + local-presence
    caller ID). Inbound calling, SMS/text campaigns, and live patch-in/
    double-dial conferencing are all deferred -- correction as of this
    comment: there is currently no inbound webhook anywhere in
    routers/dialer.py, despite an earlier version of this docstring
    claiming one existed. A number with no inbound call handler
    configured falls back to whatever default SignalWire's own dashboard
    assigns it (typically nothing useful) -- inbound is not silently
    working today, it is simply unbuilt."""

    OUTBOUND = "outbound"


class DialerCallStatus(str, Enum):
    """Raw technical call-progress status from SignalWire's status-callback
    webhook (Twilio-compatible values) -- purely mechanical (did the call
    connect), not a judgment about lead quality. See DialerDisposition for
    the human decision that drives the pipeline purge/advance."""

    QUEUED = "queued"
    RINGING = "ringing"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    BUSY = "busy"
    NO_ANSWER = "no-answer"
    FAILED = "failed"
    CANCELED = "canceled"


class DialerDisposition(str, Enum):
    """The human call-outcome decision made from the campaign dashboard
    after a call attempt -- mirrors the existing silo Convert/Dismiss
    pattern (see pipeline.convert_or_update_silo_candidate). Set once per
    DialerCallAttempt; nothing here is inferred automatically from call
    audio/sentiment -- that would require live NLU this phase doesn't
    build. ADVANCE either converts a silo candidate into a Master Log V2
    lead or advances an existing Master Log lead's status; PURGE dismisses
    a silo candidate or marks a Master Log lead attrited, exactly like the
    manual buttons already do."""

    UNSET = "unset"
    ADVANCE = "advance"
    PURGE = "purge"


class DialerCallDirection(str, Enum):
    """OUTBOUND = existing Phase 1 behavior (place_outbound_call). INBOUND
    = a merchant calling one of your SignalWire numbers back -- see
    routers/dialer.py's /webhooks/inbound and services/dialer.py's
    ring-group logic."""

    OUTBOUND = "outbound"
    INBOUND = "inbound"


class LeadContributionReason(str, Enum):
    """Why a co_broker was added to LeadContributor for a lead beyond its
    single primary MasterLogEntry.co_broker -- an additive credit ledger,
    never a replacement for the primary assignment. Deliberately a real
    enum (not free text) so the reason is queryable for commission-split
    reporting later, the same reasoning as every other enum in this
    codebase over a notes-field convention."""

    INBOUND_CALL_ANSWERED = "inbound_call_answered"
