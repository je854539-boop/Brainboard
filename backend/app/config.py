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

    cme_globex_api_key: str = ""
    import_genius_api_key: str = ""
    seavantage_api_key: str = ""
    regrid_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
