# Brainboard

Institutional alternative credit arbitrage dashboard and macro-surveillance
intelligence grid. FastAPI + PostgreSQL(pgvector) backend, Tailwind CSS +
HTMX + Alpine.js frontend, bidirectional Google Sheets/Calendar/Drive sync.

No telephony/SignalWire integration is included by design.

## Architecture

```
backend/
  app/
    models/           SQLAlchemy models + hardcoded enums (18 co-brokers,
                       13 pipeline statuses, 9 macro silos)
    services/
      hazard_engine.py     Kaplan-Meier survival curves + Markov transition matrix
      pipeline.py          shared lead-creation / status-transition / silo-conversion logic
      telemetry/            CME Globex, Import Genius, SeaVantage, UCC filings,
                             SOS registries, Regrid adapters (inert until an API key
                             or state endpoint is configured)
      google/
        sheets_sync.py       bidirectional Master Log V2 + 9 silo tab sync
        calendar_sync.py     Column K/L -> Calendar event sync via lead_uid
        drive_harvester.py   `[Business Name] | [UUID]/FINANCIALS` OCR harvester
    routers/            dashboard pages, HTMX partials, JSON API, Apps Script webhooks
    templates/           Jinja2 + Tailwind + HTMX + Alpine, tactical/military UI
  alembic/               DB migrations
  seed.py                 demo data generator (dev/staging only)
apps_script/
  Code.gs                Google Apps Script: doPost intake, onMasterLogEdit,
                          onSiloStatusEdit -- calls the FastAPI /webhooks/* routes
```

### Frontend dependencies are fully vendored -- no runtime CDN calls

`app/static/vendor/{htmx,alpine}`, `app/static/fonts/jetbrains-mono/`, and
`app/static/css/tailwind.css` are committed, built artifacts. The dashboard
never fetches from unpkg/cdn.tailwindcss.com/fonts.googleapis.com at
runtime, so it keeps working on a VPS behind a locked-down egress policy.
Rebuild them after changing a template's utility classes or bumping a
vendored version:

```
cd backend
npm install
npm run vendor-assets   # re-copies htmx/Alpine/fonts from node_modules
npm run build-css       # rebuilds app/static/css/tailwind.css
```

## Local development

Requires Python 3.11+, PostgreSQL 16 with the `pgvector` extension available,
and Node 18+ (for the one-time asset build above).

```
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp ../.env.example ../.env   # then edit values
export DATABASE_URL=postgresql+psycopg://brainboard:brainboard@localhost:5432/brainboard

alembic upgrade head
python seed.py               # optional: populate demo data
uvicorn app.main:app --reload
```

Visit `http://localhost:8000`.

## VPS deployment

```
git clone <this repo> && cd Brainboard
cp .env.example .env   # fill in real secrets
docker compose up -d --build
docker compose exec api alembic upgrade head
```

`docker-compose.yml` runs Postgres (`pgvector/pgvector:pg16`) and the API.
Put a reverse proxy (nginx/Caddy) with TLS in front of the `api` service for
production.

## Configuring integrations

Everything is inert (no-ops, not errors) until configured:

- **Google Sheets/Calendar/Drive**: set `GOOGLE_SERVICE_ACCOUNT_FILE` to a
  service-account key with Sheets/Calendar/Drive scopes, shared with edit
  access on the Master Log V2 spreadsheet, the target Calendar, and the
  Drive dossier root folder. Set `GOOGLE_MASTER_LOG_SHEET_ID`,
  `GOOGLE_CALENDAR_ID`, `GOOGLE_DRIVE_ROOT_FOLDER_ID`.
- **Telemetry providers**: set `CME_GLOBEX_API_KEY`, `IMPORT_GENIUS_API_KEY`,
  `SEAVANTAGE_API_KEY`, `REGRID_API_KEY`. UCC filings / SOS registries are
  per-state -- configure `STATE_ENDPOINTS` in
  `app/services/telemetry/{ucc_filings,sos_registries}.py`.
- **Apps Script**: open the Master Log V2 spreadsheet's Apps Script editor,
  paste in `apps_script/Code.gs`, set Script Properties `BACKEND_BASE_URL`
  and `WEBHOOK_SHARED_SECRET` (must match the backend's
  `WEBHOOK_SHARED_SECRET`), then run `setupTriggers()` once from the editor
  to install the installable `onEdit` trigger. Deploy the script as a Web
  App (Execute as: Me, Access: Anyone with the link) to get a URL for
  inbound `doPost` lead intake.

## Data model notes / column mapping assumptions

Only Master Log V2 Column K (follow-up date), Column L (notes), and Column X
(dossier link) are fixed by spec; the rest of the A-X mapping in
`app/services/google/sheets_sync.py::MASTER_LOG_COLUMN_FIELDS` is a
reasonable default that should be confirmed against your actual header row
before going live. The 8-column silo tab schema (Phone at Col C, Dossier at
Col F) matches the spec exactly.

## Hazard engine

`app/services/hazard_engine.py` fits real Kaplan-Meier survival curves
(via `lifelines`) and an empirical Markov transition matrix from the
`status_history` ledger -- there is no synthetic/simulated math. Curves are
flat/empty until there's enough status-transition history to fit against;
run `python seed.py` in dev to see them populated.
