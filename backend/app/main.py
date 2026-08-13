from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import (
    analytics,
    brain,
    calls,
    dashboard,
    enrichment,
    globe,
    master_log,
    partials,
    silo,
    surveillance,
    webhooks,
)
from app.services import river_surveillance


@asynccontextmanager
async def lifespan(app: FastAPI):
    # No-ops unless RIVER_SURVEILLANCE_ENABLED=true -- see
    # river_surveillance.start_background_polling's docstring.
    river_surveillance.start_background_polling()
    yield
    river_surveillance.stop_background_polling()


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


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
