from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.models.enums import CoBroker, MasterLogStatus, SiloName

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["dashboard"])


@router.get("/")
def index():
    return RedirectResponse(url="/master-log")


@router.get("/intake")
def intake_page(request: Request):
    return templates.TemplateResponse(
        request,
        "intake.html",
        {"co_brokers": list(CoBroker), "statuses": list(MasterLogStatus), "active_nav": "intake"},
    )


@router.get("/master-log")
def master_log_page(request: Request):
    return templates.TemplateResponse(
        request,
        "master_log.html",
        {"co_brokers": list(CoBroker), "statuses": list(MasterLogStatus), "active_nav": "master-log"},
    )


@router.get("/silo-grid")
def silo_grid_page(request: Request):
    return templates.TemplateResponse(
        request,
        "silo_grid.html",
        {"silos": list(SiloName), "active_nav": "silo-grid"},
    )


@router.get("/analytics")
def analytics_page(request: Request):
    return templates.TemplateResponse(request, "analytics.html", {"active_nav": "analytics"})


@router.get("/surveillance")
def surveillance_page(request: Request):
    return templates.TemplateResponse(request, "surveillance.html", {"active_nav": "surveillance"})


@router.get("/enrichment")
def enrichment_page(request: Request):
    return templates.TemplateResponse(request, "enrichment.html", {"active_nav": "enrichment"})


@router.get("/brain")
def brain_page(request: Request):
    return templates.TemplateResponse(request, "brain.html", {"active_nav": "brain"})


@router.get("/globe")
def globe_page(request: Request):
    return templates.TemplateResponse(
        request, "globe.html", {"active_nav": "globe", "cesium_ion_token": get_settings().cesium_ion_token}
    )


@router.get("/calls")
def calls_page(request: Request):
    return templates.TemplateResponse(request, "calls.html", {"active_nav": "calls"})


@router.get("/dialer")
def dialer_page(request: Request):
    return templates.TemplateResponse(
        request,
        "dialer.html",
        {"active_nav": "dialer", "co_brokers": list(CoBroker), "statuses": list(MasterLogStatus), "silos": list(SiloName)},
    )
