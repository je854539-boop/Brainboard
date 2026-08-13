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

    cobalt_intelligence_api_key: str = ""
    interzoid_api_key: str = ""
    apollo_api_key: str = ""
    openfda_api_key: str = ""  # optional -- openFDA works unauthenticated at low volume
    deepgram_api_key: str = ""

    brain_shadow_mode_lead_threshold: int = 1000

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
