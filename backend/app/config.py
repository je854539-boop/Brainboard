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


@lru_cache
def get_settings() -> Settings:
    return Settings()
