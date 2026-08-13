"""Autonomous maritime & river telemetry observation engine.

Polls USACE lock telemetry and Datalastic AIS positions on a background
schedule, cross-references active vessels against Import Genius/SeaVantage
manifest data to identify cargo owners, computes a per-zone congestion
("friction") score and a learned baseline transit/dwell duration per zone,
and runs a two-rule trigger matrix (distress / expansion) that appends an
audit-trailed note to the matched lead in Master Log V2 -- same
Sheet-push/Calendar-sync/activity-logging path every other pipeline
mutation uses (see services/pipeline.py).

HONESTY NOTE on USACE LPMS: the request that produced this file names two
specific endpoints -- "Lock Queue Flotilla Report" and "Lock Status
Report" -- as the USACE Lock Performance Monitoring System's REST
surface. I do not have a confirmed source for that exact endpoint shape.
This sandbox cannot reach any `*.usace.army.mil` host to check live docs,
and unlike the CWMS Data API (verified earlier this project against
USACE's own open-source client, https://github.com/USACE/cwms-data-api),
I have no equivalent source to verify "LPMS" against. Guessing a URL and
labeling it real is the exact mistake an earlier version of
telemetry/usace.py made and had to be corrected for -- so rather than
repeat it, lock status/queue data here is sourced from the two channels
already verified in that file:
  - poll_usace_lock_status() -- real CWMS gate-change activity (the "Lock
    Status Report" equivalent: a cluster of gate changes means active
    water-control intervention, i.e. the lock is restricting traffic).
  - poll_usace_lock_queue() -- an AIS-proxy idle-vessel-cluster detector
    (the "Lock Queue Flotilla Report" equivalent: idle barges/towboats
    clustered at a lock's coordinates mean a queue has formed).
Same real-world signal (queue length, delay, closures) as what was asked
for; honestly-sourced plumbing under it. If LPMS turns out to expose a
real, documented REST API, swap these two functions' HTTP calls for it --
the rest of the engine (snapshot storage, friction index, trigger matrix)
is source-agnostic and doesn't need to change.

Datalastic's `/vessel_find` (search by MMSI/IMO/name) and the Import
Genius / SeaVantage cargo cross-reference calls carry the same
"unconfirmed against live docs" caveat already attached to every other
adapter for these three providers elsewhere in this project -- see
telemetry/datalastic.py, telemetry/import_genius.py,
telemetry/seavantage.py.

Entity resolution (_resolve_entity) goes through
app/services/entity_matching.py: a cheap DB-side blocking-token
pre-filter narrows candidates, then Interzoid fuzzy company-name scoring
(when INTERZOID_API_KEY is set) confirms the match, catching legal-name
variants a raw substring check would miss; falls back to substring
matching otherwise, same as silo_leadgen.py's Cobalt/Apollo stages.
"""

import asyncio
import datetime as dt
import logging
import math
import re

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import (
    ActivitySource,
    GeofenceEventType,
    TelemetrySource,
    WaterwayTriggerType,
)
from app.models.orm import (
    GeofenceEvent,
    MasterLogEntry,
    SiloCandidate,
    WaterwayFrictionMetric,
    WaterwayTelemetrySnapshot,
    WaterwayTrigger,
)
from app.services import entity_matching, pipeline
from app.services.telemetry.usace import (
    CWMS_BASE_URL,
    GATE_CHANGE_LOOKBACK_HOURS,
    GATE_CHANGE_THRESHOLD,
    MONITORED_LOCKS,
)

logger = logging.getLogger("brainboard.river_surveillance")

# ---------------------------------------------------------------------------
# Reference geodata -- monitored waterway zones
# ---------------------------------------------------------------------------

# Reuses the Mississippi/Illinois grain-corridor locks already defined in
# telemetry/usace.py and extends them with the Ohio River and Gulf
# Intracoastal corridors + the Montreal/St. Lawrence Seaway vector named
# in the spec (Gulf/Corpus Christi/Port Arthur up to Midwest hubs).
# Coordinates are public port/lock locations -- same reference-geodata
# confidence level as the globe's ports.json, not confirmed against a
# live USACE catalog. `is_lock` gates the lock-queue-threshold check
# below: idle vessels are a real disruption signal at a lock, but normal,
# expected behavior at an open port waiting on a berth.
WATERWAY_ZONES: list[dict] = [
    *[
        {
            **lock,
            "corridor": "Mississippi" if "Mississippi" in lock["name"] else "Illinois Waterway",
            "restricted": True,
            "is_lock": True,
        }
        for lock in MONITORED_LOCKS
    ],
    {"name": "McAlpine Locks (Ohio River, Louisville)", "lat": 38.264, "lon": -85.833, "office": "", "project_id": "", "corridor": "Ohio", "restricted": True, "is_lock": True},
    {"name": "Port Arthur / Sabine-Neches Waterway (Gulf Intracoastal)", "lat": 29.897, "lon": -93.930, "office": "", "project_id": "", "corridor": "Gulf Intracoastal", "restricted": True, "is_lock": False},
    {"name": "Port of Corpus Christi Ship Channel", "lat": 27.813, "lon": -97.393, "office": "", "project_id": "", "corridor": "Gulf Intracoastal", "restricted": True, "is_lock": False},
    {"name": "Montreal / St. Lawrence Seaway", "lat": 45.550, "lon": -73.550, "office": "", "project_id": "", "corridor": "St. Lawrence Seaway", "restricted": False, "is_lock": False},
]

QUEUE_RADIUS_NM = 3
QUEUE_THRESHOLD = 3  # idle vessels clustered at a LOCK zone = a queue, not routine traffic
ZONE_MATCH_RADIUS_NM = 10  # how close a vessel_find position must be to a zone to be attributed to it

DATALASTIC_BASE_URL = "https://api.datalastic.com/api/v0"  # placeholder -- confirm against your Datalastic API plan
VESSELFINDER_BASE_URL = "https://api.vesselfinder.com"  # placeholder -- confirm against your VesselFinder API plan
IMPORT_GENIUS_BASE_URL = "https://www.importgenius.com/api"  # placeholder -- confirm against your Import Genius API plan
SEAVANTAGE_BASE_URL = "https://api.seavantage.com"  # placeholder -- confirm against your SeaVantage API plan


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * earth_radius_nm * math.asin(math.sqrt(a))


def _nearest_zone(lat: float | None, lon: float | None, max_nm: float = ZONE_MATCH_RADIUS_NM) -> dict | None:
    if lat is None or lon is None:
        return None
    best, best_dist = None, None
    for zone in WATERWAY_ZONES:
        distance = _haversine_nm(float(lat), float(lon), zone["lat"], zone["lon"])
        if best_dist is None or distance < best_dist:
            best, best_dist = zone, distance
    return best if best_dist is not None and best_dist <= max_nm else None


def _blocking_token(name: str) -> str:
    """First alphanumeric word of a company name, used as a cheap DB-side
    pre-filter ("blocking key" in record-linkage terms) before the real
    fuzzy comparison -- pulling every lead/candidate into Python to
    fuzzy-score against would work but doesn't scale. A single-token LIKE
    is deliberately loose (catches 'Heartland Grain Exporters LLC' as a
    candidate for 'Heartland Grain Co') and lets entity_matching.names_match
    make the actual same-entity call."""
    words = re.findall(r"[A-Za-z0-9]+", name)
    return words[0].lower() if words else name.strip().lower()


def _resolve_entity(db: Session, company_name: str | None):
    """Entity resolution against known leads and silo candidates: a
    cheap blocking-token pre-filter narrows the search, then
    entity_matching.names_match (Interzoid fuzzy scoring when
    configured, substring fallback otherwise) confirms the match. There's
    no shared entity ID between vessel-cargo data and the pipeline, so
    this is always a name match, not a guaranteed join. Same matching
    utility as silo_leadgen.py's Cobalt/Apollo stages."""
    if not company_name or not company_name.strip():
        return None

    token = _blocking_token(company_name)
    interzoid_key = get_settings().interzoid_api_key

    leads = db.query(MasterLogEntry).filter(func.lower(MasterLogEntry.business_name).like(f"%{token}%")).limit(15).all()
    for lead in leads:
        if entity_matching.names_match(interzoid_key, company_name, lead.business_name):
            return lead.lead_uid

    candidates = db.query(SiloCandidate).filter(func.lower(SiloCandidate.company_name).like(f"%{token}%")).limit(15).all()
    for candidate in candidates:
        if entity_matching.names_match(interzoid_key, company_name, candidate.company_name):
            return candidate.candidate_uid

    return None


def _apply_trigger_note(db: Session, entity_uid, trigger: WaterwayTrigger) -> None:
    """Requirement: every trigger that resolves to a real lead appends a
    detailed, source-attributed audit-trail line to that lead's notes,
    via the same pipeline.update_lead_fields path every other mutation
    uses -- so Sheet push, Calendar sync, and activity logging all fire
    normally, no separate write path to keep in sync."""
    lead = db.get(MasterLogEntry, entity_uid)
    if lead is None:
        return  # entity_uid resolved to a SiloCandidate, not a materialized lead -- trigger stays recorded in waterway_triggers only
    timestamp = _now().strftime("%Y-%m-%d %H:%M UTC")
    audit_line = f"[River Surveillance {timestamp}] {trigger.trigger_type.value}: {trigger.detail}"
    new_notes = f"{lead.notes}\n{audit_line}" if lead.notes else audit_line
    pipeline.update_lead_fields(db, lead, {"notes": new_notes}, source=ActivitySource.SYSTEM)


# ---------------------------------------------------------------------------
# 1. Integration sources -- USACE LPMS (real CWMS + AIS-proxy, see module
#    docstring), Datalastic AIS, Import Genius / SeaVantage cargo cross-ref
# ---------------------------------------------------------------------------


def poll_usace_lock_status(db: Session) -> list[GeofenceEvent]:
    """'Lock Status Report' equivalent -- real CWMS gate-change activity
    per monitored lock. Skips any zone without a confirmed office/
    project_id, same as telemetry/usace.py's own gate-change channel."""
    settings = get_settings()
    lock_zones = [z for z in WATERWAY_ZONES if z.get("office") and z.get("project_id")]
    if not lock_zones:
        return []

    headers = {"Authorization": f"apikey {settings.cwms_api_key}"} if settings.cwms_api_key else {}
    end = _now()
    begin = end - dt.timedelta(hours=GATE_CHANGE_LOOKBACK_HOURS)

    events: list[GeofenceEvent] = []
    with httpx.Client(timeout=30) as client:
        for zone in lock_zones:
            try:
                response = client.get(
                    f"{CWMS_BASE_URL}/projects/{zone['office']}/{zone['project_id']}/gate-changes",
                    params={"begin": begin.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")},
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:  # noqa: BLE001 -- one bad zone must not break the sweep
                logger.warning("USACE lock-status poll failed for %s: %s", zone["name"], exc)
                continue

            changes = payload if isinstance(payload, list) else payload.get("gate-changes", [])
            if len(changes) >= GATE_CHANGE_THRESHOLD:
                event = GeofenceEvent(
                    zone_name=zone["name"],
                    event_type=GeofenceEventType.LOCK_CLOSURE,
                    detail=f"{len(changes)} gate changes in {GATE_CHANGE_LOOKBACK_HOURS}h -- active water-control intervention",
                    source_reference=f"cwms:{zone['office']}/{zone['project_id']}/gate-changes",
                )
                db.add(event)
                events.append(event)
    db.commit()
    return events


def _vessels_near(client: httpx.Client, lat: float, lon: float, settings) -> list[dict]:
    if settings.datalastic_api_key:
        response = client.get(
            f"{DATALASTIC_BASE_URL}/vessel_inradius",
            params={"api-key": settings.datalastic_api_key, "lat": lat, "lon": lon, "radius": QUEUE_RADIUS_NM},
        )
        response.raise_for_status()
        payload = response.json()
        vessels = payload.get("data", {}).get("vessels") if isinstance(payload.get("data"), dict) else payload.get("vessels", [])
        return vessels or []
    if settings.vesselfinder_api_key:
        delta = 0.05  # roughly QUEUE_RADIUS_NM at mid-latitudes
        bbox = f"{lon - delta},{lat - delta},{lon + delta},{lat + delta}"
        response = client.get(f"{VESSELFINDER_BASE_URL}/vessels", params={"userkey": settings.vesselfinder_api_key, "bbox": bbox})
        response.raise_for_status()
        payload = response.json()
        vessels = payload if isinstance(payload, list) else payload.get("vessels", payload.get("data", []))
        return vessels or []
    return []


def poll_usace_lock_queue(db: Session) -> list[GeofenceEvent]:
    """'Lock Queue Flotilla Report' equivalent -- AIS-proxy idle-vessel
    clustering, same technique as telemetry/usace.py's channel 2, but run
    across every monitored zone (not just locks) so a single sweep also
    populates WaterwayTelemetrySnapshot rows for the Gulf/Seaway zones
    that feed velocity-anomaly and zone-transition detection below. The
    idle-cluster -> LOCK_QUEUE_DELAY threshold only fires for `is_lock`
    zones: idle vessels are a disruption signal at a lock, but normal
    behavior at an open port waiting on a berth."""
    settings = get_settings()
    if not (settings.datalastic_api_key or settings.vesselfinder_api_key):
        return []

    source = TelemetrySource.DATALASTIC if settings.datalastic_api_key else TelemetrySource.VESSELFINDER
    events: list[GeofenceEvent] = []

    with httpx.Client(timeout=30) as client:
        for zone in WATERWAY_ZONES:
            try:
                vessels = _vessels_near(client, zone["lat"], zone["lon"], settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Vessel-position poll failed for %s: %s", zone["name"], exc)
                continue

            idle_count = 0
            for vessel in vessels:
                speed = vessel.get("speed", vessel.get("SPEED"))
                mmsi = vessel.get("mmsi", vessel.get("MMSI"))
                db.add(
                    WaterwayTelemetrySnapshot(
                        source=source,
                        zone_name=zone["name"],
                        mmsi=str(mmsi) if mmsi else None,
                        vessel_name=vessel.get("name", vessel.get("NAME")),
                        latitude=vessel.get("lat", vessel.get("LAT")),
                        longitude=vessel.get("lon", vessel.get("LON")),
                        speed_knots=speed,
                        payload=vessel,
                    )
                )
                if speed is not None and speed <= settings.river_surveillance_idle_speed_knots:
                    idle_count += 1

            if zone.get("is_lock") and idle_count >= QUEUE_THRESHOLD:
                event = GeofenceEvent(
                    zone_name=zone["name"],
                    event_type=GeofenceEventType.LOCK_QUEUE_DELAY,
                    detail=f"{idle_count} idle vessels queued (AIS proxy)",
                    source_reference=f"vessel_inradius:{zone['name']}",
                )
                db.add(event)
                events.append(event)
    db.commit()
    return events


def poll_datalastic_vessel_find(db: Session, mmsi_list: list[str]) -> list[WaterwayTelemetrySnapshot]:
    """Real-time position/speed/draft lookup for specific vessels by
    MMSI, via Datalastic's documented vessel_find endpoint (a targeted
    lookup, distinct from vessel_inradius's area search already used
    elsewhere in this project)."""
    settings = get_settings()
    if not settings.datalastic_api_key or not mmsi_list:
        return []

    snapshots: list[WaterwayTelemetrySnapshot] = []
    with httpx.Client(base_url=DATALASTIC_BASE_URL, timeout=30) as client:
        for mmsi in mmsi_list:
            try:
                response = client.get("/vessel_find", params={"api-key": settings.datalastic_api_key, "mmsi": mmsi})
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Datalastic vessel_find failed for MMSI %s: %s", mmsi, exc)
                continue

            vessel = payload.get("data", payload) if isinstance(payload, dict) else {}
            if not vessel:
                continue
            zone = _nearest_zone(vessel.get("lat"), vessel.get("lon"))
            snapshot = WaterwayTelemetrySnapshot(
                source=TelemetrySource.DATALASTIC,
                zone_name=zone["name"] if zone else "unassigned",
                mmsi=str(mmsi),
                imo=str(vessel["imo"]) if vessel.get("imo") else None,
                vessel_name=vessel.get("name"),
                latitude=vessel.get("lat"),
                longitude=vessel.get("lon"),
                speed_knots=vessel.get("speed"),
                draft_meters=vessel.get("draft"),
                payload=vessel,
            )
            db.add(snapshot)
            snapshots.append(snapshot)
    db.commit()
    return snapshots


def cross_reference_cargo(mmsi: str | None, imo: str | None) -> list[dict]:
    """Look up bill-of-lading / manifest records for a vessel by MMSI/IMO
    via Import Genius and SeaVantage, to identify cargo owner and
    upstream/downstream flow. Both endpoints below are unconfirmed
    against live docs (see telemetry/import_genius.py,
    telemetry/seavantage.py) -- same honest-placeholder posture as those
    adapters."""
    settings = get_settings()
    if not mmsi and not imo:
        return []

    records: list[dict] = []

    if settings.import_genius_api_key:
        try:
            with httpx.Client(base_url=IMPORT_GENIUS_BASE_URL, headers={"X-Api-Key": settings.import_genius_api_key}, timeout=30) as client:
                response = client.get("/v1/shipments", params={"mmsi": mmsi, "imo": imo})
                response.raise_for_status()
                for item in response.json().get("results", []):
                    records.append({"source": "import_genius", **item})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Import Genius cargo cross-reference failed for mmsi=%s imo=%s: %s", mmsi, imo, exc)

    if settings.seavantage_api_key:
        try:
            with httpx.Client(base_url=SEAVANTAGE_BASE_URL, headers={"Authorization": f"Bearer {settings.seavantage_api_key}"}, timeout=30) as client:
                response = client.get("/v1/vessels", params={"mmsi": mmsi, "imo": imo})
                response.raise_for_status()
                for item in response.json().get("vessels", []):
                    records.append({"source": "seavantage", **item})
        except Exception as exc:  # noqa: BLE001
            logger.warning("SeaVantage cargo cross-reference failed for mmsi=%s imo=%s: %s", mmsi, imo, exc)

    return records


# ---------------------------------------------------------------------------
# 2. The Brain's observation & learning engine
# ---------------------------------------------------------------------------


def detect_velocity_anomalies(db: Session) -> list[GeofenceEvent]:
    """'A cargo vessel or towboat dropping to 0 knots for > 4 hours in a
    restricted channel' per spec. Scans recent snapshots per (mmsi, zone)
    for the longest unbroken run of idle-speed readings ending now, and
    flags it once that run exceeds river_surveillance_velocity_anomaly_hours
    inside a restricted zone."""
    settings = get_settings()
    idle_speed = settings.river_surveillance_idle_speed_knots
    anomaly_hours = settings.river_surveillance_velocity_anomaly_hours
    lookback = _now() - dt.timedelta(hours=72)
    restricted_zones = {z["name"] for z in WATERWAY_ZONES if z.get("restricted")}

    mmsi_zone_pairs = (
        db.query(WaterwayTelemetrySnapshot.mmsi, WaterwayTelemetrySnapshot.zone_name)
        .filter(WaterwayTelemetrySnapshot.observed_at >= lookback, WaterwayTelemetrySnapshot.mmsi.isnot(None))
        .distinct()
        .all()
    )

    events: list[GeofenceEvent] = []
    for mmsi, zone_name in mmsi_zone_pairs:
        if zone_name not in restricted_zones:
            continue

        snapshots = (
            db.query(WaterwayTelemetrySnapshot)
            .filter(
                WaterwayTelemetrySnapshot.mmsi == mmsi,
                WaterwayTelemetrySnapshot.zone_name == zone_name,
                WaterwayTelemetrySnapshot.observed_at >= lookback,
            )
            .order_by(WaterwayTelemetrySnapshot.observed_at.desc())
            .all()
        )
        if not snapshots or snapshots[0].speed_knots is None or float(snapshots[0].speed_knots) > idle_speed:
            continue  # not currently idle -- no anomaly to report right now

        idle_since = snapshots[0].observed_at
        for snap in snapshots:
            if snap.speed_knots is not None and float(snap.speed_knots) <= idle_speed:
                idle_since = snap.observed_at
            else:
                break

        duration_hours = (_now() - idle_since).total_seconds() / 3600
        if duration_hours < anomaly_hours:
            continue

        already_flagged = (
            db.query(GeofenceEvent)
            .filter(
                GeofenceEvent.mmsi == mmsi,
                GeofenceEvent.zone_name == zone_name,
                GeofenceEvent.event_type == GeofenceEventType.VELOCITY_ANOMALY,
                GeofenceEvent.detected_at >= idle_since,
            )
            .first()
        )
        if already_flagged:
            continue  # same idle episode already recorded -- don't re-flag every sweep

        event = GeofenceEvent(
            zone_name=zone_name,
            event_type=GeofenceEventType.VELOCITY_ANOMALY,
            mmsi=mmsi,
            vessel_name=snapshots[0].vessel_name,
            speed_knots=snapshots[0].speed_knots,
            stationary_minutes=int(duration_hours * 60),
            detail=f"MMSI {mmsi} at/below {idle_speed}kt for {duration_hours:.1f}h in restricted zone '{zone_name}'",
            source_reference=f"waterway_telemetry_snapshot:{snapshots[0].id}",
        )
        db.add(event)
        events.append(event)

    db.commit()
    return events


def detect_zone_transitions(db: Session) -> list[GeofenceEvent]:
    """Route-pattern signal: compares each vessel's two most recent
    snapshots to detect a zone entry, cross-references cargo to attempt
    entity resolution, and records it -- this is what "dock frequency"
    counts against for the expansion trigger below."""
    lookback = _now() - dt.timedelta(hours=6)
    mmsi_rows = (
        db.query(WaterwayTelemetrySnapshot.mmsi)
        .filter(WaterwayTelemetrySnapshot.observed_at >= lookback, WaterwayTelemetrySnapshot.mmsi.isnot(None))
        .distinct()
        .all()
    )

    events: list[GeofenceEvent] = []
    for (mmsi,) in mmsi_rows:
        snapshots = (
            db.query(WaterwayTelemetrySnapshot)
            .filter(WaterwayTelemetrySnapshot.mmsi == mmsi, WaterwayTelemetrySnapshot.observed_at >= lookback)
            .order_by(WaterwayTelemetrySnapshot.observed_at.desc())
            .limit(2)
            .all()
        )
        if len(snapshots) < 2 or snapshots[0].zone_name == snapshots[1].zone_name:
            continue  # no transition this window

        latest = snapshots[0]
        already = (
            db.query(GeofenceEvent)
            .filter(
                GeofenceEvent.mmsi == mmsi,
                GeofenceEvent.event_type == GeofenceEventType.ZONE_ENTRY,
                GeofenceEvent.zone_name == latest.zone_name,
                GeofenceEvent.detected_at >= latest.observed_at - dt.timedelta(minutes=1),
            )
            .first()
        )
        if already:
            continue

        cargo = cross_reference_cargo(mmsi, latest.imo)
        company_name = next((c.get("consignee_name") or c.get("company_name") for c in cargo if c.get("consignee_name") or c.get("company_name")), None)
        entity_uid = _resolve_entity(db, company_name)

        event = GeofenceEvent(
            zone_name=latest.zone_name,
            event_type=GeofenceEventType.ZONE_ENTRY,
            mmsi=mmsi,
            imo=latest.imo,
            vessel_name=latest.vessel_name,
            speed_knots=latest.speed_knots,
            entity_uid=entity_uid,
            detail=f"MMSI {mmsi} entered zone '{latest.zone_name}' (from '{snapshots[1].zone_name}')"
            + (f" -- cargo cross-ref: {company_name}" if company_name else ""),
            source_reference=f"waterway_telemetry_snapshot:{latest.id}",
        )
        db.add(event)
        events.append(event)

    db.commit()
    return events


def compute_port_dwell_hours(db: Session, mmsi: str, zone_name: str) -> float | None:
    """How long a vessel has been continuously sitting in `zone_name`,
    measured from its most recent ZONE_ENTRY into that zone -- distinct
    from the velocity-anomaly episode duration above (which only counts
    time at/below idle speed). A vessel can dwell at a port for days at
    low-but-nonzero speed (maneuvering, waiting on a berth) without ever
    tripping the idle-speed velocity-anomaly check. Returns None if the
    vessel's latest snapshot shows it's no longer in this zone (it's
    moved on -- nothing to measure) or there's no recorded entry."""
    latest_snapshot = (
        db.query(WaterwayTelemetrySnapshot)
        .filter(WaterwayTelemetrySnapshot.mmsi == mmsi)
        .order_by(WaterwayTelemetrySnapshot.observed_at.desc())
        .first()
    )
    if latest_snapshot is None or latest_snapshot.zone_name != zone_name:
        return None

    last_entry = (
        db.query(GeofenceEvent)
        .filter(
            GeofenceEvent.mmsi == mmsi,
            GeofenceEvent.zone_name == zone_name,
            GeofenceEvent.event_type == GeofenceEventType.ZONE_ENTRY,
        )
        .order_by(GeofenceEvent.detected_at.desc())
        .first()
    )
    if last_entry is None:
        return None

    return (_now() - last_entry.detected_at).total_seconds() / 3600


def classify_vessel_state(db: Session, mmsi: str, zone_name: str, speed_knots: float | None) -> str:
    """Green/amber/red classification for the globe's live vessel
    overlay -- broader than the funded-lead trigger matrix above (this
    applies to every tracked vessel, not just ones cross-referenced to a
    company). Returns "distress" (red, pulsing), "friction" (amber), or
    "normal" (green):
      - distress: >= river_surveillance_distress_idle_hours (36h)
        stationary in a restricted zone (same threshold
        check_distress_trigger uses to judge fundability), OR >=
        river_surveillance_port_dwell_days (10d) continuously dwelling
        at the same zone regardless of speed.
      - friction: idle-speed right now but hasn't crossed either
        threshold yet.
      - normal: everything else.
    """
    settings = get_settings()
    idle_speed = settings.river_surveillance_idle_speed_knots
    zone = next((z for z in WATERWAY_ZONES if z["name"] == zone_name), None)
    restricted = bool(zone and zone.get("restricted"))
    is_idle_now = speed_knots is not None and speed_knots <= idle_speed

    if restricted and is_idle_now:
        anomaly = (
            db.query(GeofenceEvent)
            .filter(
                GeofenceEvent.mmsi == mmsi,
                GeofenceEvent.zone_name == zone_name,
                GeofenceEvent.event_type == GeofenceEventType.VELOCITY_ANOMALY,
            )
            .order_by(GeofenceEvent.detected_at.desc())
            .first()
        )
        if anomaly and anomaly.stationary_minutes and (anomaly.stationary_minutes / 60) >= settings.river_surveillance_distress_idle_hours:
            return "distress"

    dwell_hours = compute_port_dwell_hours(db, mmsi, zone_name)
    if dwell_hours is not None and (dwell_hours / 24) >= settings.river_surveillance_port_dwell_days:
        return "distress"

    if is_idle_now:
        return "friction"

    return "normal"


def learn_baseline_transit(db: Session, zone_name: str) -> float | None:
    """Route pattern recognition: learns a zone's 'normal' dwell/idle
    duration from historical velocity-anomaly episodes over the trailing
    baseline window -- the median stationary duration, in hours. Returns
    None until at least 3 episodes exist, same 'don't fabricate a
    baseline from near-zero data' posture as the Brain's own
    MIN_TRAINING_EXAMPLES gate."""
    window_start = _now() - dt.timedelta(days=get_settings().river_surveillance_baseline_window_days)
    durations = [
        row[0]
        for row in db.query(GeofenceEvent.stationary_minutes)
        .filter(
            GeofenceEvent.zone_name == zone_name,
            GeofenceEvent.stationary_minutes.isnot(None),
            GeofenceEvent.detected_at >= window_start,
        )
        .all()
    ]
    if len(durations) < 3:
        return None

    durations.sort()
    mid = len(durations) // 2
    median_minutes = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2
    return round(median_minutes / 60, 2)


def compute_friction_index(db: Session, zone_name: str) -> WaterwayFrictionMetric:
    """Composite 0-100 congestion/friction score for one zone over a
    48h lookback: closures weigh heaviest (traffic stops outright), lock
    queue events next, velocity anomalies least (weaker evidence on
    their own). This is a first-pass heuristic, not a calibrated model --
    validate the weights against real trigger-to-funded outcomes once
    live telemetry is flowing, same posture as the CME Macro Funnel's
    price-shock threshold."""
    lookback = _now() - dt.timedelta(hours=48)

    def _count(event_type: GeofenceEventType) -> int:
        return (
            db.query(GeofenceEvent)
            .filter(GeofenceEvent.zone_name == zone_name, GeofenceEvent.event_type == event_type, GeofenceEvent.detected_at >= lookback)
            .count()
        )

    closures = _count(GeofenceEventType.LOCK_CLOSURE)
    queue_events = _count(GeofenceEventType.LOCK_QUEUE_DELAY)
    anomalies = _count(GeofenceEventType.VELOCITY_ANOMALY)
    baseline = learn_baseline_transit(db, zone_name)

    score = min(100.0, closures * 20.0 + queue_events * 10.0 + anomalies * 5.0)

    metric = WaterwayFrictionMetric(
        zone_name=zone_name,
        friction_score=score,
        active_queue_count=queue_events,
        velocity_anomaly_count=anomalies,
        baseline_transit_hours=baseline,
    )
    db.add(metric)
    db.commit()
    return metric


# ---------------------------------------------------------------------------
# Trigger matrix
# ---------------------------------------------------------------------------

_TRIGGER_DEDUPE_WINDOW = dt.timedelta(hours=24)  # don't re-fire the same ongoing condition every 15-minute sweep


def check_distress_trigger(db: Session, mmsi: str, zone_name: str, idle_hours: float, cargo_records: list[dict]) -> list[WaterwayTrigger]:
    """Distress trigger: inbound raw-material manifest matches a target
    company AND the vessel has been delayed/idle in a USACE queue/zone
    for longer than river_surveillance_distress_idle_hours (36h per
    spec)."""
    if idle_hours < get_settings().river_surveillance_distress_idle_hours:
        return []

    triggers: list[WaterwayTrigger] = []
    seen_companies: set[str] = set()
    for record in cargo_records:
        company_name = record.get("consignee_name") or record.get("company_name")
        if not company_name or company_name in seen_companies:
            continue
        seen_companies.add(company_name)

        recent = (
            db.query(WaterwayTrigger)
            .filter(
                WaterwayTrigger.trigger_type == WaterwayTriggerType.DISTRESS_SUPPLY_STARVATION,
                WaterwayTrigger.company_name_guess == company_name,
                WaterwayTrigger.zone_name == zone_name,
                WaterwayTrigger.created_at >= _now() - _TRIGGER_DEDUPE_WINDOW,
            )
            .first()
        )
        if recent:
            continue

        entity_uid = _resolve_entity(db, company_name)
        trigger = WaterwayTrigger(
            trigger_type=WaterwayTriggerType.DISTRESS_SUPPLY_STARVATION,
            entity_uid=entity_uid,
            company_name_guess=company_name,
            zone_name=zone_name,
            detail=(
                f"Inbound manifest for '{company_name}' (MMSI {mmsi}, source: {record.get('source')}) matches a vessel "
                f"delayed/idle {idle_hours:.1f}h at '{zone_name}' -- exceeds the "
                f"{get_settings().river_surveillance_distress_idle_hours:.0f}h supply-starvation threshold."
            ),
            source_reference=f"cargo_cross_reference:{record.get('source')}:mmsi={mmsi}",
        )
        db.add(trigger)
        db.commit()
        if entity_uid:
            _apply_trigger_note(db, entity_uid, trigger)
        triggers.append(trigger)

    return triggers


def check_expansion_trigger(db: Session, entity_uid, company_name: str, zone_name: str) -> WaterwayTrigger | None:
    """Expansion trigger: current 7-day zone-entry ("dock frequency")
    rate for this entity exceeds its 90-day rolling baseline daily rate
    by river_surveillance_expansion_multiplier (1.5x default)."""
    settings = get_settings()
    window_start = _now() - dt.timedelta(days=settings.river_surveillance_baseline_window_days)
    recent_start = _now() - dt.timedelta(days=7)

    def _entry_count(start, end=None) -> int:
        query = db.query(GeofenceEvent).filter(
            GeofenceEvent.entity_uid == entity_uid,
            GeofenceEvent.event_type == GeofenceEventType.ZONE_ENTRY,
            GeofenceEvent.detected_at >= start,
        )
        if end is not None:
            query = query.filter(GeofenceEvent.detected_at < end)
        return query.count()

    baseline_count = _entry_count(window_start, recent_start)
    recent_count = _entry_count(recent_start)

    baseline_days = settings.river_surveillance_baseline_window_days - 7
    baseline_daily_rate = baseline_count / baseline_days if baseline_days > 0 else 0.0
    recent_daily_rate = recent_count / 7

    if baseline_daily_rate <= 0 or recent_daily_rate < baseline_daily_rate * settings.river_surveillance_expansion_multiplier:
        return None  # no baseline yet, or no spike -- don't fabricate a signal from insufficient history

    recent_fire = (
        db.query(WaterwayTrigger)
        .filter(
            WaterwayTrigger.trigger_type == WaterwayTriggerType.EXPANSION_THROUGHPUT_SPIKE,
            WaterwayTrigger.entity_uid == entity_uid,
            WaterwayTrigger.zone_name == zone_name,
            WaterwayTrigger.created_at >= _now() - _TRIGGER_DEDUPE_WINDOW,
        )
        .first()
    )
    if recent_fire:
        return None

    trigger = WaterwayTrigger(
        trigger_type=WaterwayTriggerType.EXPANSION_THROUGHPUT_SPIKE,
        entity_uid=entity_uid,
        company_name_guess=company_name,
        zone_name=zone_name,
        detail=(
            f"'{company_name}' dock-visit rate at '{zone_name}' is {recent_daily_rate:.2f}/day over the trailing 7 days vs a "
            f"{baseline_daily_rate:.2f}/day {settings.river_surveillance_baseline_window_days}-day baseline "
            f"({recent_daily_rate / baseline_daily_rate:.1f}x) -- exceeds the {settings.river_surveillance_expansion_multiplier:.1f}x threshold."
        ),
        source_reference=f"geofence_zone_entry_rate:{entity_uid}:{zone_name}",
    )
    db.add(trigger)
    db.commit()
    _apply_trigger_note(db, entity_uid, trigger)
    return trigger


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_full_sweep(db: Session) -> dict:
    """One complete synchronous pass: USACE lock status + queue,
    velocity-anomaly detection, zone-transition tracking, trigger matrix,
    friction index per zone. Mirrors the summary-dict convention of
    silo_leadgen.run_silo_leadgen -- callable directly (see
    scripts/verify_river_surveillance.py) or from the async polling loop
    below via asyncio.to_thread."""
    lock_status_events = poll_usace_lock_status(db)
    lock_queue_events = poll_usace_lock_queue(db)
    velocity_events = detect_velocity_anomalies(db)
    zone_entry_events = detect_zone_transitions(db)

    distress_triggers: list[WaterwayTrigger] = []
    for event in velocity_events:
        if not event.mmsi:
            continue
        idle_hours = (event.stationary_minutes or 0) / 60
        cargo = cross_reference_cargo(event.mmsi, event.imo)
        distress_triggers.extend(check_distress_trigger(db, event.mmsi, event.zone_name, idle_hours, cargo))

    expansion_triggers: list[WaterwayTrigger] = []
    recent_entity_zones = (
        db.query(GeofenceEvent.entity_uid, GeofenceEvent.zone_name)
        .filter(
            GeofenceEvent.entity_uid.isnot(None),
            GeofenceEvent.event_type == GeofenceEventType.ZONE_ENTRY,
            GeofenceEvent.detected_at >= _now() - dt.timedelta(days=7),
        )
        .distinct()
        .all()
    )
    for entity_uid, zone_name in recent_entity_zones:
        lead = db.get(MasterLogEntry, entity_uid)
        company_name = lead.business_name if lead is not None else str(entity_uid)
        trigger = check_expansion_trigger(db, entity_uid, company_name, zone_name)
        if trigger:
            expansion_triggers.append(trigger)

    friction_metrics = [compute_friction_index(db, zone["name"]) for zone in WATERWAY_ZONES]

    return {
        "lock_status_events": len(lock_status_events),
        "lock_queue_events": len(lock_queue_events),
        "velocity_anomaly_events": len(velocity_events),
        "zone_entry_events": len(zone_entry_events),
        "distress_triggers": len(distress_triggers),
        "expansion_triggers": len(expansion_triggers),
        "friction_metrics_computed": len(friction_metrics),
        "zones_monitored": len(WATERWAY_ZONES),
    }


# ---------------------------------------------------------------------------
# Background polling -- must not block the FastAPI event loop
# ---------------------------------------------------------------------------

_polling_task: asyncio.Task | None = None


async def _polling_loop() -> None:
    from app.database import SessionLocal

    while True:
        try:
            db = SessionLocal()
            try:
                summary = await asyncio.to_thread(run_full_sweep, db)
                logger.info("River surveillance sweep complete: %s", summary)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 -- one bad cycle must not kill the loop
            logger.exception("River surveillance sweep failed -- will retry next cycle")
        await asyncio.sleep(get_settings().river_surveillance_poll_interval_seconds)


def start_background_polling() -> asyncio.Task | None:
    """Starts the 15-minute polling loop as a background asyncio task.
    Each sweep's actual network/DB work runs inside asyncio.to_thread
    (the rest of this codebase's adapters and the ORM session are
    synchronous), so the worker thread blocks -- not the event loop,
    which stays free to serve requests while a sweep is in flight.
    No-ops (and logs why) unless RIVER_SURVEILLANCE_ENABLED=true, so no
    background network polling starts without an explicit opt-in."""
    global _polling_task
    if not get_settings().river_surveillance_enabled:
        logger.info("River surveillance disabled (set RIVER_SURVEILLANCE_ENABLED=true to activate)")
        return None
    if _polling_task is None or _polling_task.done():
        _polling_task = asyncio.create_task(_polling_loop())
        logger.info("River surveillance background polling started (interval=%ss)", get_settings().river_surveillance_poll_interval_seconds)
    return _polling_task


def stop_background_polling() -> None:
    global _polling_task
    if _polling_task is not None:
        _polling_task.cancel()
        _polling_task = None
        logger.info("River surveillance background polling stopped")
