from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import (
    analytics,
    brain,
    calls,
    dashboard,
    dialer,
    enrichment,
    globe,
    master_log,
    partials,
    silo,
    surveillance,
    webhooks,
)
from app.routers import river_surveillance as river_surveillance_router
from app.services import brain as brain_service
from app.services import enrichment_orchestrator
from app.services import river_surveillance
from app.services.google import calendar_sync


@asynccontextmanager
async def lifespan(app: FastAPI):
    # No-ops unless RIVER_SURVEILLANCE_ENABLED=true -- see
    # river_surveillance.start_background_polling's docstring.
    river_surveillance.start_background_polling()
    # On by default -- see brain.start_background_refresh's docstring.
    brain_service.start_background_refresh()
    # On by default -- see enrichment_orchestrator.start_background_sweep's docstring.
    enrichment_orchestrator.start_background_sweep()
    # Off unless CALENDAR_CONTINUOUS_PULL_ENABLED=true -- see
    # calendar_sync.start_background_pull's docstring.
    calendar_sync.start_background_pull()
    yield
    river_surveillance.stop_background_polling()
    brain_service.stop_background_refresh()
    enrichment_orchestrator.stop_background_sweep()
    calendar_sync.stop_background_pull()


app = FastAPI(title="Brainboard Command Deck", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(dashboard.router)
app.include_router(partials.router)
app.include_router(master_log.router)
app.include_router(silo.router)
app.include_router(analytics.router)
app.include_router(surveillance.router)
app.include_router(webhooks.router)
app.include_router(enrichment.router)
app.include_router(brain.router)
app.include_router(globe.router)
app.include_router(calls.router)
app.include_router(dialer.router)
app.include_router(river_surveillance_router.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
