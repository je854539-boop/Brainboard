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
                       13 pipeline statuses, 9 macro silos, 18 data-provider sources)
    services/
      hazard_engine.py     Kaplan-Meier survival curves + Markov transition matrix +
                            time-varying Cox model (fit on the activity ledger)
      brain.py              shadow-mode logistic-regression funded-probability scorer
      pipeline.py           shared lead-creation / field-update / status-transition /
                             silo-conversion / activity-logging / Calendar+Sheet sync logic
      silo_leadgen.py        derives SiloCandidate rows from telemetry sweeps -- see
                             SOURCE_TO_SILOS for which provider feeds which silo(s);
                             CME Macro Funnel alone is fed by 12 of the 15 macro
                             telemetry providers (see "Silo Grid" below)
      enrichment_orchestrator.py   CSV lead import + per-lead enrichment runner
      call_analysis.py       Deepgram Nova post-call transcription/diarization/sentiment
      river_surveillance.py  autonomous maritime/river telemetry engine -- USACE lock
                             status/queue + Datalastic AIS polling, velocity-anomaly and
                             zone-transition detection, friction-index scoring, and a
                             distress/expansion trigger matrix that appends audit-trailed
                             notes to matched leads (see "River Surveillance" below)
      telemetry/            15 macro-sweep adapters (CME Globex, Import Genius,
                             SeaVantage, UCC filings, SOS registries, Regrid,
                             HigherGov, openFDA, Datalastic, VesselFinder, GDELT,
                             GFW 4Wings, Cobalt Intelligence, Apollo.io, USACE),
                             inert until an API key/state endpoint/watch-list is
                             configured
      enrichment/            Cobalt Intelligence, Interzoid, Apollo.io, openFDA,
                             Deepgram Nova, CME Globex, Import Genius, SeaVantage,
                             Regrid, HigherGov, GFW 4Wings, GDELT, Datalastic,
                             VesselFinder -- 14 providers run per lead at intake to
                             surface what the dialer call missed
      globe/                 GFW 4Wings + Datalastic + VesselFinder (marine traffic,
                             AIS) + GDELT (conflict zones) geospatial adapters
      google/
        sheets_sync.py       bidirectional Master Log V2 + 9 silo tab sync
        calendar_sync.py     Column K/L and status changes -> Calendar event sync via lead_uid
        drive_harvester.py   `[Business Name] | [UUID]/FINANCIALS` OCR harvester
    routers/            dashboard pages, HTMX partials, JSON API, Apps Script webhooks
    templates/           Jinja2 + Tailwind + HTMX + Alpine, tactical/military UI --
                         Intake, Master Log, Silo Grid, Enrichment, Calls, Brain, Globe,
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
  new SiloCandidate rows from what it ingests. 7 of the 9 silos use a flat
  1-event-to-1-candidate mapping (`app/services/silo_leadgen.py::SOURCE_TO_SILOS`)
  -- the right model when one provider's record IS the lead signal (a UCC
  filing, an SOS status change, an openFDA recall).

  **CME Macro Funnel** and **Agriculture & Grain Handling** are not flat --
  both are genuine sequential waterfalls, because a price move alone isn't a
  lead and a company name alone isn't a lead.

  **CME Macro Funnel** (`run_cme_macro_funnel_waterfall()`) -- "Chicago
  commodities market swings and crashes" is a dependency chain, not one
  signal:
  1. **Signal** -- CME Globex price-shock (>=2% intraday move on the CBOT
     grain/oilseed complex) + GDELT commodity-shock news + GFW 4Wings ocean/
     vessel-traffic anomalies. No active signal = the waterfall stops here,
     zero candidates created.
  2. **Identify** -- Import Genius customs manifests + Regrid parcel data
     (address-matched, see below) turn the signal into actual company names,
     corroborated (not identified) by fresh SeaVantage/Datalastic/
     VesselFinder/USACE vessel and barge activity in the same window --
     USACE covers Midwest river-port-dependent businesses specifically
     (gate-change activity + AIS barge queuing at Mississippi/Illinois
     Waterway locks).
  3. **Filter** -- Cobalt Intelligence SOS standing: a company in suspended/
     dissolved standing is dismissed here regardless of signal strength,
     since it isn't fundable no matter how loud the macro signal is.
  4. **Contact** -- Apollo.io org search attaches phone/contact info to
     whatever survives the filter.

  **Agriculture & Grain Handling** (`run_agriculture_grain_handling_waterfall()`)
  -- a narrower, more direct version of the same pattern. CME Globex's
  tracked symbols are already scoped to just the CBOT grain/oilseed complex,
  and USACE's monitored locks default to the Mississippi/Illinois grain
  corridor, so for this silo specifically both are direct operating-condition
  indicators for grain handlers, not just leading indicators of a broader
  macro trend:
  1. **Signal** -- CME Globex grain-price shock + USACE grain-corridor
     disruption (gate-change activity or barge queuing).
  2. **Identify** -- Regrid parcel data, address-matched (see below).

  **Regrid address matching** (`_identify_via_regrid()`, shared by both
  waterfalls) -- a parcel's owner-of-record is a land-ownership fact, not
  proof that the owner is the business operating there (a tenant doesn't
  own the land it leases). Identification is address-first: the parcel's
  site address is matched against a business address on file from Import
  Genius, and that matched company is surfaced -- not the landowner. When
  no address match is found, the parcel owner is still surfaced rather than
  dropped, but flagged explicitly in notes as land-ownership-only /
  operator-unconfirmed, so a converted lead's provenance is honest about
  which case it was.

  Every waterfall candidate's `notes` field records exactly which signal
  gated it in, which trade-data source identified it, and (for CME Macro
  Funnel) what the lending-appetite check found -- nothing is a black box.
- **River Surveillance** (`app/services/river_surveillance.py`) -- an
  autonomous background engine, separate from the Silo Grid's manual sweep
  button, that polls USACE lock telemetry and Datalastic AIS positions on a
  15-minute schedule (configurable) and runs a distress/expansion trigger
  matrix against them. Off by default -- set `RIVER_SURVEILLANCE_ENABLED=true`
  to activate; no background network polling starts without that explicit
  opt-in. See `.env.example` for the rest of its config knobs.

  **On the "LPMS" endpoints**: the spec that produced this engine named two
  specific USACE Lock Performance Monitoring System endpoints ("Lock Queue
  Flotilla Report", "Lock Status Report"). That exact REST surface isn't
  confirmed against live docs -- this sandbox can't reach `*.usace.army.mil`
  to check, and unlike the CWMS Data API (verified against USACE's own
  https://github.com/USACE/cwms-data-api), there's no equivalent source to
  verify "LPMS" against. Rather than guess a URL and label it real, lock
  status/queue data is sourced from the same two verified channels already
  in `telemetry/usace.py`: real CWMS gate-change activity (`LOCK_CLOSURE`)
  and an AIS-proxy idle-vessel-cluster detector (`LOCK_QUEUE_DELAY`). Same
  real-world signal, honestly-sourced plumbing -- swap in a real LPMS client
  later if one turns out to exist; the rest of the engine doesn't need to
  change.

  **Zones monitored**: the Mississippi/Illinois locks from
  `telemetry/usace.py::MONITORED_LOCKS`, plus McAlpine Locks (Ohio River),
  Port Arthur/Sabine-Neches and Corpus Christi Ship Channel (Gulf
  Intracoastal), and the Montreal/St. Lawrence Seaway -- see
  `river_surveillance.py::WATERWAY_ZONES`.

  **Observation & learning loop**:
  - *Route pattern recognition* (`detect_zone_transitions`) -- tracks
    vessel zone entries, cross-referencing Import Genius/SeaVantage cargo
    data by MMSI/IMO to resolve the entity operating each vessel.
  - *Friction index* (`compute_friction_index`) -- a 0-100 composite
    congestion score per zone over a 48h window (closures weigh heaviest,
    lock queues next, velocity anomalies least) -- a first-pass heuristic,
    not a calibrated model; validate the weights against real
    trigger-to-funded outcomes once live telemetry is flowing.
  - *Baseline learning* (`learn_baseline_transit`) -- median historical
    dwell duration per zone; returns `None` until at least 3 episodes
    exist rather than fabricating a baseline from near-zero data, same
    posture as the Brain's `MIN_TRAINING_EXAMPLES` gate.
  - *Trigger matrix*: **Distress** (supply starvation) fires when an
    inbound raw-material manifest matches a company AND that company's
    vessel has been idle/queued >36h (`RIVER_SURVEILLANCE_DISTRESS_IDLE_HOURS`)
    in a restricted zone. **Expansion** (throughput spike) fires when a
    company's trailing 7-day dock-visit rate exceeds its 90-day rolling
    baseline by 1.5x (`RIVER_SURVEILLANCE_EXPANSION_MULTIPLIER`). Both
    dedupe against the same ongoing condition for 24h so an unresolved
    delay doesn't refire every 15-minute sweep.

  **Persistence & audit trail**: raw vessel/lock observations land in
  `waterway_telemetry_snapshots`; classified events (zone entry/exit,
  velocity anomaly, lock queue delay, lock closure) in `geofence_events`;
  per-zone congestion scores in `waterway_friction_metrics`; trigger-matrix
  firings in `waterway_triggers`. Every trigger that resolves to a real
  lead (best-effort name match against Master Log V2, no shared entity ID
  between vessel-cargo data and the pipeline) appends a source-attributed
  audit line to that lead's `notes` via the same `pipeline.update_lead_fields`
  path every other mutation uses -- so Sheet push, Calendar sync, and
  activity logging all fire normally.

  **Non-blocking by design**: the actual network/DB work for each 15-minute
  sweep runs inside `asyncio.to_thread`, so a sweep in flight never blocks
  the FastAPI event loop from serving requests. Run
  `python3 scripts/verify_river_surveillance.py` to see this proven end to
  end against mock USACE/Datalastic payloads and a concurrent event-loop
  heartbeat, on a real Postgres instance.
- **Enrichment** (`/enrichment`) -- CSV lead upload and per-lead enrichment
  against all 14 providers (Cobalt Intelligence, Interzoid, Apollo.io,
  openFDA, Deepgram Nova, CME Globex, Import Genius, SeaVantage, Regrid,
  HigherGov, GFW 4Wings, GDELT, Datalastic, VesselFinder) -- the point is
  surfacing what a dialer call didn't cover before the follow-up. CME
  Globex and GFW 4Wings aren't company-searchable APIs, so their
  "enrichment" is macro/regional context attached to the lead rather than
  a personalized lookup; Datalastic and VesselFinder are vessel-identity
  APIs, so their "enrichment" is a best-effort vessel-name match against
  the business name, not a confirmed company-to-fleet lookup -- both
  caveats flagged directly in the result payload.
- **Calls** (`/calls`) -- upload a recorded call (or paste a URL to one
  already hosted) and Deepgram Nova transcribes, diarizes, summarizes, and
  sentiment-scores it. Analyzes recordings after the fact only -- no
  telephony integration, it doesn't place/receive/route calls. Optionally
  tag a call with a lead so it lands on that lead's activity ledger.
- **Brain** (`/brain`) -- the shadow-mode scoring engine (see below).
- **Globe** (`/globe`) -- a true 3D globe rendered with **CesiumJS**
  (vendored locally like everything else, no runtime CDN), replacing the
  earlier Three.js implementation. Plots:
  - **Every Master Log V2 lead**, color-coded and animated by its exact
    pipeline status -- see "13-status pipeline" below. Leads are placed
    at their state's approximate centroid (`app/services/globe_geo.py`)
    since Master Log V2 stores a state, not a geocoded address; several
    leads in the same state render near the same point.
  - **Live river/AIS vessel traffic** from the River Surveillance engine
    (`/api/river-surveillance/vessels`), green/amber/red by
    `river_surveillance.py::classify_vessel_state`.
  - GFW 4Wings/Datalastic/VesselFinder marine-traffic signals and GDELT
    conflict-zone events (last 24h) -- the original `globe_signals` feed,
    unchanged.
  - Static reference layers (unchanged data, re-rendered through Cesium's
    entity API instead of Three.js): country borders (Natural Earth 50m),
    state/province ("admin-1") borders for every country Natural Earth
    tracks them for, major trade corridors, ~45 major ocean container
    ports, ~23 USACE inland river ports, and all 241 national capitals.
    Port/capital name labels use Cesium's native `DistanceDisplayCondition`
    (only render once zoomed in close) instead of the old custom
    CSS2D zoom-gating logic.

  Click any lead or vessel to orbit-dive in (`camera.flyTo`) and open a
  detail panel with its real audit trail and behavioral signals --
  **not biometric data**: this system has no biometric data source
  anywhere, so nothing here is labeled or implied to be one. What's
  actually shown is real: Master Log activity-ledger event counts, the
  Brain's latest funded-probability score, Deepgram call sentiment when
  available, and the lead's `notes` field (which River Surveillance
  triggers already append themselves to).

  Without a `CESIUM_ION_TOKEN` configured, the globe still renders fully
  -- using Cesium's own bundled offline Natural Earth II imagery (lower
  resolution, no terrain) instead of failing to show a basemap at all.
  Set `CESIUM_ION_TOKEN` (from your own Cesium ion account -- this
  project can't generate one for you) for full-resolution satellite
  imagery and real terrain. Note this is inherently a client-visible
  token, not a hidden server secret -- ion tokens are meant to be used in
  browser JS and restricted by referrer/domain in your ion dashboard.

  #### 13-status pipeline visualization

  Every `MasterLogStatus` enum value maps 1:1 to a color and animation
  (verified by an automated check against the real enum, not just
  eyeballed -- see `app/templates/globe.html`'s `STATUS_STYLE`):

  | Status | Color | Animation |
  |---|---|---|
  | New lead | Ghost Gray `#A0AEC0` | small static node |
  | App Sent | Electric Blue `#00B4D8` | steady pulse |
  | Docs Owed | Warning Amber `#FFB703` | slow pulse |
  | Chase Docs | Deep Orange `#FB8500` | rapid flash |
  | Docs in | Vivid Lime Green `#38A3A5` | solid anchor |
  | In negotiation | Electric Violet `#7209B7` | spinning halo (a real rotating billboard, not a simulated spin -- Cesium points have no orientation, billboards do) |
  | Offer Made Not Sold | Hot Magenta `#F72585` | neon ring |
  | Sold Deal Killed | Muted Slate `#4A5568` | graveyard node (dim, static) |
  | Deal Stalled Proxy Pass | Toxic Yellow-Green `#CCFF00` | intermittent flicker |
  | Funded | Gold Beacon `#4CC9F0` | crown-jewel beacon pulse, **and** a real live-updating line to any vessel whose cargo cross-reference resolved to this lead (see River Surveillance's `_resolve_entity`) -- not cosmetic, an actual data link |
  | Ghosted | Dark Industrial Steel `#2D333B` | low-opacity node |
  | Loss to Competitor | Blood Crimson `#D90429` | X-marker |
  | Dog Shit | Dead Black `#121417` | hidden by default -- "Show Dog Shit (filtered)" checkbox reveals it |

  There is a genuine *live* USACE signal in the app beyond this page too
  -- see the CME Macro Funnel entry in "Silo Grid" below for the
  lock-congestion adapter, and "River Surveillance" for the full engine.

  #### Silo funnel lifecycle (creation -> Master Log V2 -> Funded)

  A diamond marker layer, separate from the 13-status lead layer above,
  tracks `SiloCandidate` rows from creation through conversion:
  amber = pending, teal = converted, gray = dismissed
  (`SILO_CANDIDATE_COLOR` in `globe.html`). Once a candidate converts, a
  dashed line traces from its original detection point to the resulting
  lead's position -- the same entity then continues the thread through
  the 13-status pipeline colors above it, and if it reaches Funded, the
  beacon + live supply-chain link.

  This layer is necessarily a *subset*: `SiloCandidate` has no address
  field, and most silo sources (UCC filings, SOS registries, openFDA,
  Cobalt Intelligence, Apollo) carry no confirmed geo signal in their
  payload at all. Only candidates identified via **Regrid** (CME Macro
  Funnel and Agriculture & Grain Handling's waterfalls, see
  `_regrid_geometry_centroid` in `silo_leadgen.py`) get real coordinates,
  extracted from the parcel's own GeoJSON geometry -- not a guessed
  location. Everything else stays honestly unplaced rather than being
  shown at a fabricated point. `GET /api/silo/geo` serves this layer.

  **Relationship to the Brain**: none, currently, and worth saying
  plainly -- this whole globe (13-status colors, the silo lifecycle
  layer, all of it) is a pure visualization sitting on top of data the
  Brain already had. A lead's status color reflects a `StatusHistory` row
  that already existed before the globe rendered it; the globe doesn't
  feed the logistic regression a new signal, it just makes an existing
  one visible. If you want the globe to actually feed the Brain something
  new (geographic clustering, corridor friction at time of funding), that
  would be a separate, unbuilt enhancement.
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

`app/static/vendor/{htmx,alpine,cesium}`, `app/static/fonts/jetbrains-mono/`,
and `app/static/css/tailwind.css` are committed, built artifacts. The
dashboard never fetches from unpkg/cdn.tailwindcss.com/fonts.googleapis.com/
cesium.com at runtime, so it keeps working on a VPS behind a locked-down
egress policy -- `vendor-assets.js` copies Cesium's entire pre-built
`Build/Cesium` output (Cesium.js + Widgets CSS + Assets/Workers/ThirdParty,
~23MB) the same way it copies htmx/Alpine. The one runtime exception is
Cesium ion's own imagery/terrain *tile* streaming when `CESIUM_ION_TOKEN`
is set -- that data genuinely can't be vendored (it's not static reference
geometry, it's the whole planet's satellite imagery), so the globe makes
live requests to Cesium's servers for it. Without a token it falls back to
Cesium's bundled offline imagery instead, staying fully local.
Rebuild them after changing a template's utility classes or bumping a
vendored version:

```
cd backend
npm install
npm run vendor-assets      # re-copies htmx/Alpine/fonts from node_modules
npm run build-css          # rebuilds app/static/css/tailwind.css
npm run build-geo          # rebuilds app/static/data/country-borders.json
npm run build-globe-extras # rebuilds sea-routes/ports/usace-ports/capitals JSON
npm run build-admin1       # rebuilds app/static/data/admin1-borders.json
```

`build-geo`, `build-globe-extras`, and `build-admin1` only need to be
re-run if you bump the `world-atlas` resolution or edit the corridor/port
lists in `scripts/build-globe-extras.js` -- the generated JSON is
committed, so a fresh clone doesn't need Node (or network access) at
deploy time for the globe to work. `build-globe-extras` additionally
depends on the `all-the-cities` (GeoNames capital-city dump) and
`world-countries` devDependencies, used only at build time to produce
`capitals.json`. `build-admin1` fetches Natural Earth's 50m admin-1
boundary-lines dataset directly from `raw.githubusercontent.com` at
build time (see the script header for a manual-download fallback if
that host isn't reachable from your build environment).

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
- **Telemetry providers** (macro silo sweeps): set `DATABENTO_API_KEY`,
  `IMPORT_GENIUS_API_KEY`, `SEAVANTAGE_API_KEY`, `REGRID_API_KEY`,
  `HIGHERGOV_API_KEY`, `DATALASTIC_API_KEY`, `VESSELFINDER_API_KEY`,
  `GFW_API_KEY`. `OPENFDA_API_KEY` and `GDELT_API_KEY` are optional (both
  work unauthenticated/keyless at normal volume). `OPENFDA_API_KEY` feeds
  Healthcare & Pharma Silo lead-gen -- a recall is treated as a
  financing-need signal. SeaVantage/Datalastic/VesselFinder feed
  Oil & Gas / Refining Silo lead-gen (AIS vessel activity as a
  financing-need signal for maritime-adjacent leads), and the same three
  plus GDELT/GFW 4Wings/USACE/Cobalt Intelligence/Apollo.io all feed
  **CME Macro Funnel** lead-gen -- see the Silo Grid section above for
  why. Some of those need extra setup beyond an API key. USACE runs two
  independent channels, either of which is enough to activate it: (1) the
  real CWMS Data API (confirmed against USACE's own
  [cwms-data-api](https://github.com/USACE/cwms-data-api) source, not
  guessed) reporting gate-change operational activity per reservoir
  project -- GET requests are keyless per USACE's FAQ, `CWMS_API_KEY` is
  optional; what's required is filling in each `MONITORED_LOCKS` entry's
  real `office`/`project_id` in `app/services/telemetry/usace.py` (left
  blank by default -- self-serve lookup instructions are in that file's
  module docstring, since I can't confirm the exact CWMS project IDs
  without a live session); (2) an AIS proxy off the
  `DATALASTIC_API_KEY`/`VESSELFINDER_API_KEY` you already set, detecting
  idle barges queuing at a lock's coordinates. Defaults to grain-corridor
  locks on the Mississippi/Illinois Waterway. Cobalt Intelligence's
  telemetry sweep is empty until you populate `MONITORED_SEARCHES` in
  `app/services/telemetry/cobalt_intelligence.py` -- it's a per-entity
  lookup API with no bulk endpoint, so the sweep works by looping a
  configured watch-list of (state, search-term) pairs.
  UCC filings / SOS registries are per-state -- configure
  `STATE_ENDPOINTS` in `app/services/telemetry/{ucc_filings,sos_registries}.py`.
  `DATABENTO_API_KEY` powers CME Globex (get one at databento.com --
  Databento is a licensed redistributor of the CME Globex MDP 3.0 feed
  over plain HTTPS, avoiding the direct-exchange license + colocation a
  raw CME market-data agreement would require; verified against the real
  [databento-python](https://github.com/databento/databento-python)
  client source, not guessed -- see
  `app/services/telemetry/cme_globex.py`). It sweeps daily OHLCV bars for
  the CBOT grain/oilseed continuous front-month contracts (corn, soybeans,
  wheat, soybean meal, soybean oil) that anchor the "Chicago commodities
  market swings and crashes" framing.
- **Enrichment providers** (per-lead lookups, run at intake against leads
  sourced off the dialer): `COBALT_INTELLIGENCE_API_KEY`,
  `INTERZOID_API_KEY`, `APOLLO_API_KEY`, `OPENFDA_API_KEY` (optional),
  `DEEPGRAM_API_KEY`, plus the same `DATABENTO_API_KEY`,
  `IMPORT_GENIUS_API_KEY`, `SEAVANTAGE_API_KEY`, `REGRID_API_KEY`,
  `HIGHERGOV_API_KEY`, `GFW_API_KEY`, `DATALASTIC_API_KEY`,
  `VESSELFINDER_API_KEY` used by the telemetry/globe adapters above --
  one credential per vendor covers both its macro-sweep and
  per-lead-lookup use. `GDELT_API_KEY` is likewise shared and optional.
  `INTERZOID_API_KEY` does triple duty: per-lead enrichment as usual,
  plus cross-provider entity resolution (fuzzy company-name matching) in
  the CME Macro Funnel waterfall and the River Surveillance engine -- see
  `app/services/entity_matching.py`. Both degrade to substring matching
  without it, so nothing breaks if it's left unset, but match recall on
  legal-name variants (DBAs, LLC/Inc suffixes) will be materially worse.
- **Globe data sources**: set `GFW_API_KEY` for Global Fishing Watch
  4Wings marine traffic; `GFW_DATASET` picks which underlying dataset it
  pulls (defaults to GFW's public fishing-effort dataset -- check your
  API plan for the exact ID if you have access to broader non-fishing
  AIS traffic). `DATALASTIC_API_KEY` and `VESSELFINDER_API_KEY` add two
  more AIS vessel-position sources to the same amber marine-traffic
  layer -- both adapters ship with a default AOI (Houston Ship Channel /
  Gulf of Mexico approach) and an endpoint/response shape I couldn't
  verify against live docs in this environment, so confirm both against
  your actual API plan before relying on them (see the module docstrings
  in `app/services/{globe,telemetry,enrichment}/{datalastic,vesselfinder}.py`).
  GDELT's GEO 2.0 API (conflict-zone events) is free and keyless, so it's
  live by default with no configuration -- `GDELT_API_KEY` is reserved
  for future use only.
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
