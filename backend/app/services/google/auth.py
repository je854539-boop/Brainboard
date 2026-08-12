"""Shared Google API auth for the Sheets/Calendar/Drive sync engine.

Requires a service-account JSON key (GOOGLE_SERVICE_ACCOUNT_FILE) that has
been:
  1. Granted Editor access on the Master Log V2 spreadsheet (and each of
     the 9 silo tabs live in that same spreadsheet).
  2. Granted access to the target Google Calendar (share the calendar with
     the service account's client_email, or use domain-wide delegation).
  3. Granted access to the Drive root folder containing per-lead
     `[Business Name] | [UUID]/FINANCIALS` dossier subfolders.
"""

from functools import lru_cache

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build

from app.config import get_settings

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]


class GoogleAuthNotConfigured(RuntimeError):
    pass


@lru_cache
def get_credentials() -> service_account.Credentials:
    settings = get_settings()
    if not settings.google_service_account_file:
        raise GoogleAuthNotConfigured(
            "GOOGLE_SERVICE_ACCOUNT_FILE is not set -- Google sync is disabled until a service account key is configured."
        )
    return service_account.Credentials.from_service_account_file(settings.google_service_account_file, scopes=SCOPES)


def is_configured() -> bool:
    try:
        get_credentials()
        return True
    except GoogleAuthNotConfigured:
        return False


def sheets_service() -> Resource:
    return build("sheets", "v4", credentials=get_credentials(), cache_discovery=False)


def calendar_service() -> Resource:
    return build("calendar", "v3", credentials=get_credentials(), cache_discovery=False)


def drive_service() -> Resource:
    return build("drive", "v3", credentials=get_credentials(), cache_discovery=False)
