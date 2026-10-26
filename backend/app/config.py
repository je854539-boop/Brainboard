from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    secret_key: str = "dev-secret"
    webhook_shared_secret: str = "dev-webhook-secret"

    database_url: str = "postgresql+psycopg://brainboard:brainboard@localhost:5432/brainboard"

    google_service_account_file: str = ""
    google_master_log_sheet_id: str = ""
    google_calendar_id: str = "primary"
    google_drive_root_folder_id: str = ""

    # CME Globex MDP 3.0 futures data, via Databento (databento.com) -- a
    # licensed redistributor. Raw CME MDP 3.0 access requires a direct
    # exchange license + colocation, which Databento's plain HTTPS
    # historical API avoids. See app/services/telemetry/cme_globex.py.
    databento_api_key: str = ""
    import_genius_api_key: str = ""
    seavantage_api_key: str = ""
    regrid_api_key: str = ""
    highergov_api_key: str = ""
    gfw_api_key: str = ""  # Global Fishing Watch API token, powers the 4Wings globe layer
    # Which GFW 4Wings dataset to pull. Defaults to their best-known public
    # fishing-effort dataset; swap for a broader AIS marine-traffic dataset
    # ID per your GFW API plan -- see app/services/globe/gfw_4wings.py.
    gfw_dataset: str = "public-global-fishing-effort:latest"
    gdelt_api_key: str = ""  # optional -- GDELT's GEO 2.0 API is free/keyless at normal volume
    datalastic_api_key: str = ""  # Datalastic AIS vessel-tracking API (datalastic.com)
    vesselfinder_api_key: str = ""  # VesselFinder AIS vessel-tracking API (vesselfinder.com)
    # Optional -- USACE's real CWMS Data API (cwms-data.usace.army.mil) is
    # keyless for GET requests on non-sensitive data per their FAQ; only
    # set this if your office/endpoint combination turns out to require
    # one. The USACE adapter also uses the Datalastic/VesselFinder keys
    # above for its complementary AIS-proxy channel. See
    # app/services/telemetry/usace.py.
    cwms_api_key: str = ""
    # NOAA National Weather Service alerts (api.weather.gov) -- genuinely
    # free and keyless; no setting needed to enable it, see
    # app/services/telemetry/noaa_nws.py.
    fred_api_key: str = ""  # Federal Reserve Economic Data -- free signup key, api.stlouisfed.org
    # HONESTY NOTE: both of these are real provider categories (AAR/STB
    # rail data, ADS-B aircraft tracking) but the exact live endpoint/
    # auth shape is unconfirmed in this sandbox -- see
    # app/services/telemetry/rail_traffic.py and adsb.py. Empty key ->
    # adapter stays inert, same "no key -> no-op" posture as every other
    # adapter here.
    rail_traffic_api_key: str = ""
    adsb_api_key: str = ""
    # TransmetriQ (Railinc) -- real, confirmed product, quote-based
    # pricing (no public dollar figure; ~$2,700-$9,650/yr-scale
    # commercial relationship is the realistic range by analogy to
    # similar industry data subscriptions, not a confirmed TransmetriQ
    # number itself). Empty key -> adapter stays inert.
    transmetriq_api_key: str = ""
    # FreightWaves SONAR -- confirmed self-serve tier ~$25-35/mo; full
    # API access is enterprise/quote-based.
    sonar_api_key: str = ""
    # Planet Labs -- confirmed Monitoring-plan pricing $2,700-$9,650/yr
    # depending on coverage area (planet.com/pricing).
    planet_labs_api_key: str = ""
    # Xeneta -- confirmed subscription/enterprise pricing, quote-based,
    # no public dollar figure.
    xeneta_api_key: str = ""

    cobalt_intelligence_api_key: str = ""
    interzoid_api_key: str = ""
    apollo_api_key: str = ""
    openfda_api_key: str = ""  # optional -- openFDA works unauthenticated at low volume
    deepgram_api_key: str = ""

    # MCA-fit revenue band -- services/silo_leadgen.py's CME Macro Funnel
    # waterfall uses this to filter/dismiss candidates once Apollo.io
    # org-search telemetry carries a revenue figure for them. Tunable
    # policy, not a code constant, since this is a business decision
    # (which deal sizes you actually want), not an engineering one.
    mca_target_revenue_min_usd: float = 5_000_000.0
    mca_target_revenue_max_usd: float = 49_000_000.0

    brain_shadow_mode_lead_threshold: int = 1000

    # CesiumJS globe (templates/globe.html). Empty by default -- the
    # globe still renders (Cesium's bundled offline Natural Earth II
    # imagery, no terrain) without this, just without ion's streaming
    # high-res satellite imagery/terrain. Note this is inherently a
    # client-visible token, not a hidden server secret -- Cesium ion
    # tokens are meant to be used in browser JS and restricted by
    # referrer/domain in your ion account dashboard, not kept secret the
    # way a server-side API key is.
    cesium_ion_token: str = ""

    # River surveillance engine (app/services/river_surveillance.py) --
    # off by default so no background network polling starts without an
    # explicit opt-in. Reuses datalastic_api_key/vesselfinder_api_key/
    # cwms_api_key/import_genius_api_key/seavantage_api_key above.
    river_surveillance_enabled: bool = False
    river_surveillance_poll_interval_seconds: int = 900  # 15 minutes per spec
    # A vessel at/below this speed inside a restricted zone counts as
    # "stationary" for velocity-anomaly detection.
    river_surveillance_idle_speed_knots: float = 0.5
    # Distress trigger: minimum stationary/queue duration before an
    # inbound-manifest match is treated as supply starvation, not routine
    # transit delay.
    river_surveillance_distress_idle_hours: float = 36.0
    # Velocity-anomaly trigger: minimum stationary duration in a
    # restricted channel before it's flagged at all (independent of any
    # cargo match) -- "> 4 hours" per spec.
    river_surveillance_velocity_anomaly_hours: float = 4.0
    # Expansion trigger: rolling window used to compute a company's
    # baseline dock-visit/shipment frequency.
    river_surveillance_baseline_window_days: int = 90
    # Expansion trigger: current 7-day dock-visit rate must exceed the
    # 90-day daily baseline rate by this multiple to flag a throughput
    # spike.
    river_surveillance_expansion_multiplier: float = 1.5
    # Globe vessel-overlay red-state condition (in addition to the 36h
    # distress-idle threshold above): a vessel still docked at the same
    # port zone this many days after its last recorded zone entry.
    river_surveillance_port_dwell_days: float = 10.0
    # Multi-leg transload trigger: a coastal zone sighting and a river
    # zone sighting of the SAME company's cargo, this many days apart or
    # less, are treated as one continuous ocean-to-inland shipment rather
    # than two unrelated sightings. Real-world Gulf-to-mid-Mississippi
    # barge transit plus the transload itself realistically runs 1-3
    # weeks; 21 days gives that real travel time room without stretching
    # so wide it links two actually-unrelated shipments from the same
    # frequent importer. Tunable policy, same posture as the search-radius
    # table in silo_leadgen.py -- a reasoned default, not a measured one.
    river_surveillance_transload_window_days: int = 21
    # GDELT corroboration annotation (see persisted_macro_signals /
    # PersistedMacroSignal, river_surveillance.py::
    # _find_corroborating_signal): how old a persisted GDELT signal can
    # be and still count as "the reason" behind a live trigger. Shorter
    # than the 21-day transload window on purpose -- a transload is
    # confirming the SAME physical shipment across two legs of a real
    # multi-week transit, while a news signal explaining a behavior
    # anomaly needs to actually still be current. Tunable policy, same
    # reasoned-not-measured posture as every other window here --
    # validate against trigger_performance_report's real corroborated
    # vs. uncorroborated funded_rate once live data exists.
    river_surveillance_gdelt_corroboration_window_days: int = 14

    # SignalWire dialer (services/signalwire_adapter.py, services/dialer.py,
    # routers/dialer.py). Inert until project_id/api_token/space_url are all
    # set, same "no key -> no-op" pattern as every other adapter here.
    # SignalWire's REST API is Twilio-compatible
    # (https://developer.signalwire.com/rest/compatibility-api), reachable at
    # https://<space_url>/api/laml/2010-04-01/Accounts/<project_id>/... using
    # HTTP Basic auth (project_id as username, api_token as password).
    signalwire_project_id: str = ""
    signalwire_api_token: str = ""
    signalwire_space_url: str = ""  # e.g. "yourspace.signalwire.com" -- no scheme, no trailing slash
    # Validates inbound status-callback signatures (X-Twilio-Signature
    # compatible HMAC scheme) -- see routers/dialer.py's webhook handlers.
    # Leave unset only for local testing against a tunnel you control.
    signalwire_webhook_signing_key: str = ""
    # Publicly reachable base URL SignalWire calls back to for call-status
    # and SWML webhooks (e.g. your ngrok tunnel or production domain).
    # Required before any real outbound call can be placed -- the dialer
    # adapter refuses to place a call without it, see signalwire_adapter.py.
    dialer_public_base_url: str = ""
    # Rough real daily throughput across the whole team (all phones,
    # calls + texts combined) -- purely informational, powers the
    # capacity-vs-pending-queue indicator on the Dialer page (see
    # routers/dialer.py::capacity_summary). Never used to drop or hide
    # leads -- a queue exceeding this just gets flagged visually so a
    # human decides what to do about it, nothing is enforced silently.
    dialer_daily_capacity: int = 50

    # Brain shadow-mode scoring (services/brain.py) refreshes continuously
    # in the background instead of only on a manual "Recompute Scores"
    # click -- pure local computation (no external API/network call), so
    # unlike river surveillance this defaults to on.
    brain_continuous_refresh_enabled: bool = True
    brain_shadow_refresh_interval_seconds: int = 900  # 15 minutes

    # Enrichment sweep (services/enrichment_orchestrator.py): decouples lead
    # *creation* (must stay fast -- Apps Script's doPost blocks on it) from
    # enrichment *execution* (slow -- up to a dozen external API calls per
    # lead). Instead of running every adapter inline inside create_lead, a
    # background loop periodically finds leads with zero EnrichmentResult
    # rows yet and works through them in small batches, so a 350-lead Sheet
    # sync (or any CSV import) can't burst-hammer paid APIs or block the
    # webhook that created them.
    enrichment_continuous_sweep_enabled: bool = True
    enrichment_sweep_interval_seconds: int = 300  # 5 minutes
    enrichment_sweep_batch_size: int = 20  # leads enriched per cycle -- paces paid-API usage

    # Calendar -> Postgres pull (services/google/calendar_sync.py::pull_calendar_updates).
    # The real calendar carries call notes/deal terms that brokers type
    # directly into event descriptions and that often never make it back
    # into the Sheet -- see that module's docstring. Off by default like
    # river surveillance: it's a real external-API integration, not pure
    # local computation, so it needs an explicit opt-in once Google
    # credentials are actually configured.
    calendar_continuous_pull_enabled: bool = False
    calendar_pull_interval_seconds: int = 300  # 5 minutes
    calendar_pull_window_minutes: int = 20  # trailing window scanned each cycle -- wider than the interval so a missed cycle self-heals

    # Sheet -> Postgres full pull (services/google/sheets_sync.py::
    # pull_master_log), on a daily cadence by default -- "scan the sheet
    # every day" so the queue is computed off current balance/payment/
    # funded-amount/notes without anyone remembering to trigger
    # /api/sync/sheet/pull by hand. Same off-by-default posture as the
    # calendar pull above: a real external API call, opt in once Google
    # credentials exist.
    sheet_continuous_pull_enabled: bool = False
    sheet_pull_interval_seconds: int = 86400  # 24 hours

    # Silo lead-gen sweep (services/silo_leadgen.py::run_silo_leadgen) --
    # every signal/hunt mechanism built this session (USACE, CME, GDELT,
    # HigherGov, Apollo/Cobalt hunts) only ever ran when someone called
    # POST /api/silo/leadgen/run by hand. "A living lead-gen organism
    # that sweeps daily" means this needs to run on its own schedule,
    # same off-by-default-until-configured posture as the other real
    # external-API continuous pulls above (several of the sources this
    # sweep hits are paid APIs, not free local computation).
    silo_leadgen_continuous_sweep_enabled: bool = False
    silo_leadgen_sweep_interval_seconds: int = 86400  # 24 hours -- daily, per spec

    # LLM-based open-ended news classification (services/silo_leadgen.py::
    # _llm_classify_news) -- lets the broad GDELT sweep recognize an
    # industry it was never given a keyword list for, instead of only
    # ever matching against the fixed commodity vocabulary. Empty by
    # default -- falls back to keyword-only classification with no key
    # set, same "no key -> no-op" pattern as every other adapter.
    anthropic_api_key: str = ""
    # Cheap/fast tier is the right call here -- this runs as a
    # classification step on news headlines, potentially many per
    # sweep, not a reasoning task. Confirm this is still a live model
    # id at deploy time; model ids get retired.
    anthropic_model: str = "claude-haiku-4-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()
