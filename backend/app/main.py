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

app = FastAPI(title="Brainboard Command Deck")

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
