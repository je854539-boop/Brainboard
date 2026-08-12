import hmac
from enum import Enum

from fastapi import Header, HTTPException, status

from app.config import get_settings


def verify_webhook_secret(x_webhook_secret: str = Header(default="")) -> None:
    settings = get_settings()
    if not hmac.compare_digest(x_webhook_secret, settings.webhook_shared_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook secret")


def parse_optional_enum(value: str | None, enum_cls: type[Enum]) -> Enum | None:
    """Turns a raw '' | None query param into a real enum member or None.

    Declaring a query param as `SomeEnum | None` makes FastAPI try to
    coerce the value straight into the enum -- which 422s on the empty
    string an HTML <select><option value=""> (our "All" filter option)
    submits. Route handlers take the param as `str | None` and pass it
    through here instead.
    """
    if not value:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid value {value!r} for {enum_cls.__name__}") from exc
