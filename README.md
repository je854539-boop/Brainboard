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
                       13 pipeline statuses, 9 macro silos, 15 data-provider sources)
    services/
      hazard_engine.py     Kaplan-Meier survival curves + Markov transition matrix +
                            time-varying Cox model (fit on the activity ledger)
      brain.py              shadow-mode logistic-regression funded-probability scorer
      pipeline.py           shared lead-creation / field-update / status-transition /
                             silo-conversion / activity-logging / Calendar+Sheet sync logic
      silo_leadgen.py        derives SiloCandidate rows from telemetry sweeps
      enrichment_orchestrator.py   CSV lead import + per-lead enrichment runner
      telemetry/            CME Globex, Import Genius, SeaVantage, UCC filings,
                             SOS registries, Regrid, HigherGov adapters (macro sweeps,
                             inert until an API key or state endpoint is configured)
      enrichment/            Cobalt Intelligence, Interzoid, Apollo.io, openFDA,
                             Deepgram Nova adapters (targeted per-lead lookups)
      globe/                 GFW 4Wings (marine traffic) + GDELT (conflict zones)
                             geospatial adapters
      google/
        sheets_sync.py       bidirectional Master Log V2 + 9 silo tab sync
        calendar_sync.py     Column K/L -> Calendar event sync via lead_uid
        drive_harvester.py   `[Business Name] | [UUID]/FINANCIALS` OCR harvester
    routers/            dashboard pages, HTMX partials, JSON API, Apps Script webhooks
    templates/           Jinja2 + Tailwind + HTMX + Alpine, tactical/military UI --
                         Intake, Master Log, Silo Grid, Enrichment, Brain, Globe,
                         Analytics, Surveillance
  alembic/               DB migrations
  seed.py                 demo data generator (dev/staging only)
apps_script/
  Code.gs                Google Apps Script: doPost intake, onMasterLogEdit,
                          onSiloStatusEdit -- calls the FastAPI /webhooks/* routes
```

### Dashboard sections

- **Intake** (`/intake`) -- full MCA lead-intake form (all deal-term fields:
  revenue, lender, payment terms, balance, open positions, credit score),
  posts into Postgres via the same `pipeline.create_lead` path everything
  else uses, which pushes to the Sheet and syncs Calendar automatically.
- **Master Log** (`/master-log`) -- the pipeline command view.
- **Silo Grid** (`/silo-grid`) -- the 9 macro silos, plus a "Run Lead-Gen
  Sweep" button that runs every configured telemetry adapter and derives
  new SiloCandidate rows from what it ingests.
- **Enrichment** (`/enrichment`) -- CSV lead upload and per-lead enrichment
  against Cobalt Intelligence / Interzoid / Apollo.io / openFDA / Deepgram Nova.
- **Brain** (`/brain`) -- the shadow-mode scoring engine (see below).
- **Globe** (`/globe`) -- a spinning 3D globe (vendored Three.js) plotting
  GFW 4Wings marine traffic and GDELT conflict-zone events (last 24h).
- **Analytics** (`/analytics`) -- Kaplan-Meier curves, Markov transition
  matrix, deal-velocity bottlenecks, silo friction correlation, and the
  time-varying Cox engagement-hazard panel.
- **Surveillance** (`/surveillance`) -- live macro telemetry feed.

### The Brain (shadow-mode scoring)

`app/services/brain.py` fits a real, transparent logistic regression
(deal terms + co-broker + activity-engagement count) on whatever leads
have already resolved (Funded or attrited), then scores every lead's
funded-probability on every refresh. It runs in **SHADOW** mode --
predictions are logged to `shadow_scores` but never surface as an
actionable recommendation -- until the pipeline crosses
`BRAIN_SHADOW_MODE_LEAD_THRESHOLD` (default 1000) total leads, at which
point it flips to **LIVE**. Shadow-era predictions stay in the ledger
either way, so once a shadow-era lead resolves you can back-test how the
model would have called it. With few labeled examples the fit is
reported honestly via `training_set_size`, not hidden.

### Activity ledger ("every click, every calendar change, every note change")

`lead_activity_events` is an append-only log written by
`pipeline.log_activity` on every note change, follow-up/Column K change,
status transition, Calendar sync, UI click (dossier-link opens beacon to
`POST /api/master-log/{lead_uid}/activity`), enrichment run, and inbound
Sheet edit -- tagged with its source (`ui` / `api` / `webhook_sheet` /
`system`) so sync loops can be reasoned about. This is the same ledger the
time-varying Cox model consumes as covariates.

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
- **Telemetry providers** (macro silo sweeps): set `CME_GLOBEX_API_KEY`,
  `IMPORT_GENIUS_API_KEY`, `SEAVANTAGE_API_KEY`, `REGRID_API_KEY`,
  `HIGHERGOV_API_KEY`. UCC filings / SOS registries are per-state --
  configure `STATE_ENDPOINTS` in
  `app/services/telemetry/{ucc_filings,sos_registries}.py`.
- **Enrichment providers** (per-lead lookups): set
  `COBALT_INTELLIGENCE_API_KEY`, `INTERZOID_API_KEY`, `APOLLO_API_KEY`,
  `OPENFDA_API_KEY` (optional -- openFDA works unauthenticated),
  `DEEPGRAM_API_KEY`.
- **Globe data sources**: set `GFW_API_KEY` for Global Fishing Watch
  4Wings marine traffic; `GFW_DATASET` picks which underlying dataset it
  pulls (defaults to GFW's public fishing-effort dataset -- check your
  API plan for the exact ID if you have access to broader non-fishing
  AIS traffic). GDELT's GEO 2.0 API (conflict-zone events) is free and
  keyless, so it's live by default with no configuration -- `GDELT_API_KEY` is
  reserved for future use only.
- **Apps Script**: open the Master Log V2 spreadsheet's Apps Script editor,
  paste in `apps_script/Code.gs`, set Script Properties `BACKEND_BASE_URL`
  and `WEBHOOK_SHARED_SECRET` (must match the backend's
  `WEBHOOK_SHARED_SECRET`), then run `setupTriggers()` once from the editor
  to install the installable `onEdit` trigger. Deploy the script as a Web
  App (Execute as: Me, Access: Anyone with the link) to get a URL for
  inbound `doPost` lead intake.

## Data model notes / column mapping assumptions

Only Master Log V2 Column K (follow-up date), Column L (notes), and Column X
(dossier link) are fixed by spec. The rest of the A-X mapping in
`app/services/google/sheets_sync.py::MASTER_LOG_COLUMN_FIELDS` (and mirrored
in `apps_script/Code.gs`'s `ML_COL_*` constants) places the MCA intake
fields (state, revenue, lender, payment terms, balance, open positions,
credit score) at I/J/M-R, following the field order of the lead-intake
terminal -- a stronger signal than a blind guess since that terminal posts
straight to the real sheet, but still an inference, not a confirmed header
read. Verify column-by-column before relying on it in production. The
8-column silo tab schema (Phone at Col C, Dossier at Col F) matches the
spec exactly.

Separately: the lead-intake terminal's co-broker/status `<select>` options
are a subset of the full validated lists (missing Tony, Seb, and 4
statuses) -- the rebuilt `/intake` page in this repo uses the full 18/13
lists, but if you're still using the original standalone HTML file, its
dropdowns don't cover every valid value.

## Hazard engine

`app/services/hazard_engine.py` fits real Kaplan-Meier survival curves and
an empirical Markov transition matrix from the `status_history` ledger,
plus a time-varying Cox model (`lifelines.CoxTimeVaryingFitter`) fit on
the `lead_activity_events` ledger -- there is no synthetic/simulated math
anywhere in this module. Curves/models report "insufficient data" rather
than fabricating output until there's enough history to fit against; run
`python seed.py` in dev to see them populated (it seeds ~50 leads with a
full activity trail so the Cox model actually converges out of the box).
