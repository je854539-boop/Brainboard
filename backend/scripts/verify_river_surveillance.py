"""Verification script for the river surveillance engine
(app/services/river_surveillance.py).

Proves, against a real Postgres instance:
  1. Payload ingestion from mock USACE (CWMS gate-changes) and Datalastic
     (vessel_inradius, vessel_find) response shapes -- parsed correctly
     into typed WaterwayTelemetrySnapshot / GeofenceEvent rows.
  2. Velocity-anomaly detection (>4h idle in a restricted zone).
  3. Zone-transition tracking + cargo cross-reference + entity resolution.
  4. Friction index + baseline-transit learning.
  5. Both trigger-matrix rules (distress, expansion), including that a
     distress trigger appends an audit-trailed note to the matched lead.
  6. The background polling loop runs via asyncio.to_thread without
     blocking the event loop -- proven by a concurrent heartbeat
     coroutine that keeps ticking while a sweep is in flight.

Mocks all outbound HTTP via httpx.MockTransport (no live network calls),
since USACE/Datalastic endpoints aren't reachable from this environment
and shouldn't be hit by a verification run anyway. Run with:

    cd backend && python3 scripts/verify_river_surveillance.py
"""

import asyncio
import datetime as dt
import functools
import os
import sys
import time
import uuid
from unittest import mock

# Config must be set via env vars before any app.* import, since
# get_settings() is lru_cached and several modules read it at import time.
os.environ["DATALASTIC_API_KEY"] = "test-datalastic-key"
os.environ["IMPORT_GENIUS_API_KEY"] = "test-import-genius-key"
os.environ["CWMS_API_KEY"] = ""  # CDA GET endpoints are keyless per its FAQ
os.environ["RIVER_SURVEILLANCE_ENABLED"] = "true"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.enums import CoBroker, GeofenceEventType, WaterwayTriggerType  # noqa: E402
from app.models.orm import (  # noqa: E402
    GeofenceEvent,
    MasterLogEntry,
    WaterwayFrictionMetric,
    WaterwayTelemetrySnapshot,
    WaterwayTrigger,
)
from app.services import pipeline, river_surveillance  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    print(f"[{PASS if condition else FAIL}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        raise AssertionError(f"FAILED: {name} -- {detail}")


def reset_db(db):
    db.query(WaterwayTrigger).delete()
    db.query(GeofenceEvent).delete()
    db.query(WaterwayFrictionMetric).delete()
    db.query(WaterwayTelemetrySnapshot).delete()
    db.query(MasterLogEntry).filter(MasterLogEntry.business_name.like("Verify River%")).delete()
    db.commit()


# ---------------------------------------------------------------------------
# Mock HTTP transport -- realistic USACE CWMS / Datalastic response shapes
# ---------------------------------------------------------------------------

LOCK = river_surveillance.WATERWAY_ZONES[3]  # Lockport Lock (Illinois Waterway) -- is_lock=True, restricted=True
LOCK["office"], LOCK["project_id"] = "MVR", "LOCKPORT"  # fake but well-formed, for this test run only

IDLE_MMSI = "366123456"


def mock_handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path

    if host == "cwms-data.usace.army.mil" and path.endswith("/gate-changes"):
        # Real CWMS response shape: a list of gate-change records (see
        # telemetry/usace.py's own parsing, `changes = payload if isinstance(payload, list) else ...`).
        return httpx.Response(200, json=[{"gate": "A", "ts": "2026-08-12T10:00:00Z"}] * 4)

    if host == "api.datalastic.com" and path.endswith("/vessel_inradius"):
        return httpx.Response(
            200,
            json={
                "data": {
                    "vessels": [
                        {"mmsi": IDLE_MMSI, "name": "MV Heartland Carrier", "speed": 0.1, "lat": LOCK["lat"], "lon": LOCK["lon"]},
                        {"mmsi": "366999001", "name": "MV Idle Two", "speed": 0.0, "lat": LOCK["lat"], "lon": LOCK["lon"]},
                        {"mmsi": "366999002", "name": "MV Idle Three", "speed": 0.3, "lat": LOCK["lat"], "lon": LOCK["lon"]},
                        {"mmsi": "366999003", "name": "MV Underway", "speed": 6.2, "lat": LOCK["lat"], "lon": LOCK["lon"]},
                    ]
                }
            },
        )

    if host == "api.datalastic.com" and path.endswith("/vessel_find"):
        mmsi = request.url.params.get("mmsi")
        return httpx.Response(
            200,
            json={"data": {"mmsi": mmsi, "imo": "9123456", "name": "MV Heartland Carrier", "speed": 0.1, "lat": LOCK["lat"], "lon": LOCK["lon"], "draft": 4.2}},
        )

    if host == "www.importgenius.com" and path.endswith("/v1/shipments"):
        return httpx.Response(
            200,
            json={"results": [{"consignee_name": "Verify River Grain Co", "commodity": "corn", "bol_number": "BOL-8842"}]},
        )

    return httpx.Response(404, json={"error": "unmocked endpoint", "path": path})


mock_client_factory = functools.partial(httpx.Client, transport=httpx.MockTransport(mock_handler))


def main() -> None:
    db = SessionLocal()
    reset_db(db)

    lead = pipeline.create_lead(db, business_name="Verify River Grain Co", co_broker=CoBroker.NICK_F)
    print(f"Seeded lead: {lead.business_name} ({lead.lead_uid})\n")

    with mock.patch("httpx.Client", mock_client_factory):
        # --- 1. USACE lock status ingestion (mock CWMS gate-changes) ---
        lock_status_events = river_surveillance.poll_usace_lock_status(db)
        check("USACE lock-status ingestion creates a LOCK_CLOSURE event", len(lock_status_events) == 1)
        check(
            "LOCK_CLOSURE event references the correct zone",
            lock_status_events[0].zone_name == LOCK["name"],
        )

        # --- 1. USACE lock queue (AIS-proxy) + snapshot persistence ---
        lock_queue_events = river_surveillance.poll_usace_lock_queue(db)
        snapshot_count = db.query(WaterwayTelemetrySnapshot).count()
        check(
            "AIS-proxy poll persists one snapshot per observed vessel across all zones",
            snapshot_count == 4 * len(river_surveillance.WATERWAY_ZONES),
            f"got {snapshot_count}, expected {4 * len(river_surveillance.WATERWAY_ZONES)}",
        )
        lockport_queue = [e for e in lock_queue_events if e.zone_name == LOCK["name"]]
        check("Idle-vessel cluster at the lock zone creates a LOCK_QUEUE_DELAY event", len(lockport_queue) == 1)
        non_lock_zones_flagged = [
            e for e in lock_queue_events if not next(z for z in river_surveillance.WATERWAY_ZONES if z["name"] == e.zone_name)["is_lock"]
        ]
        check("Non-lock zones (open ports) never fire LOCK_QUEUE_DELAY even with idle vessels", len(non_lock_zones_flagged) == 0)

        # --- 1. Datalastic vessel_find targeted lookup ---
        find_snapshots = river_surveillance.poll_datalastic_vessel_find(db, [IDLE_MMSI])
        check("vessel_find ingestion creates a snapshot with parsed draft/imo", len(find_snapshots) == 1 and find_snapshots[0].draft_meters is not None and find_snapshots[0].imo == "9123456")

        # --- 1. Import Genius cargo cross-reference ---
        cargo = river_surveillance.cross_reference_cargo(IDLE_MMSI, "9123456")
        check("Cargo cross-reference resolves a consignee from the mock manifest", any(c.get("consignee_name") == "Verify River Grain Co" for c in cargo))

        # --- 2. Velocity anomaly: backfill an idle history > 4h for IDLE_MMSI ---
        now = dt.datetime.now(dt.timezone.utc)
        for hours_ago in (5, 4, 3, 2, 1, 0.1):
            db.add(
                WaterwayTelemetrySnapshot(
                    source=find_snapshots[0].source,
                    zone_name=LOCK["name"],
                    mmsi=IDLE_MMSI,
                    vessel_name="MV Heartland Carrier",
                    speed_knots=0.1,
                    observed_at=now - dt.timedelta(hours=hours_ago),
                    payload={},
                )
            )
        db.commit()
        velocity_events = river_surveillance.detect_velocity_anomalies(db)
        anomaly = next((e for e in velocity_events if e.mmsi == IDLE_MMSI), None)
        check("Velocity anomaly fires for a vessel idle >4h in a restricted zone", anomaly is not None)
        check("Velocity anomaly records a plausible stationary duration", anomaly is not None and anomaly.stationary_minutes >= 240, f"got {anomaly.stationary_minutes if anomaly else None}")

        rerun_events = river_surveillance.detect_velocity_anomalies(db)
        check("Re-running detection doesn't duplicate the same idle episode", len([e for e in rerun_events if e.mmsi == IDLE_MMSI]) == 0)

        # --- 2. Zone transitions + entity resolution ---
        db.add(
            WaterwayTelemetrySnapshot(
                source=find_snapshots[0].source, zone_name="unassigned", mmsi=IDLE_MMSI, imo="9123456",
                vessel_name="MV Heartland Carrier", speed_knots=6.0, observed_at=now - dt.timedelta(hours=6), payload={},
            )
        )
        db.commit()
        zone_events = river_surveillance.detect_zone_transitions(db)
        entry = next((e for e in zone_events if e.mmsi == IDLE_MMSI and e.event_type == GeofenceEventType.ZONE_ENTRY), None)
        check("Zone-transition detection creates a ZONE_ENTRY event", entry is not None)
        check("ZONE_ENTRY resolves entity_uid via cargo cross-reference + name match", entry is not None and entry.entity_uid == lead.lead_uid)

        # --- 2. Friction index + baseline learning ---
        metric = river_surveillance.compute_friction_index(db, LOCK["name"])
        check("Friction index is a positive composite score reflecting closures+queue+anomaly", metric.friction_score > 0)
        check("Friction metric counts match what was actually detected", metric.velocity_anomaly_count >= 1 and metric.active_queue_count >= 1)

        # --- Trigger matrix: distress ---
        distress_triggers = river_surveillance.check_distress_trigger(db, IDLE_MMSI, LOCK["name"], idle_hours=40.0, cargo_records=cargo)
        check("Distress trigger fires when idle_hours exceeds the 36h threshold with a cargo match", len(distress_triggers) == 1)
        check("Distress trigger resolves the correct entity_uid", distress_triggers[0].entity_uid == lead.lead_uid)
        db.refresh(lead)
        check(
            "Distress trigger appends a source-attributed audit line to the lead's notes",
            lead.notes is not None and "River Surveillance" in lead.notes and "distress_supply_starvation" in lead.notes,
            repr(lead.notes),
        )

        below_threshold = river_surveillance.check_distress_trigger(db, IDLE_MMSI, LOCK["name"], idle_hours=10.0, cargo_records=cargo)
        check("Distress trigger does NOT fire below the 36h threshold", len(below_threshold) == 0)

        # --- Trigger matrix: expansion ---
        for day_offset in range(90, 7, -1):
            db.add(
                GeofenceEvent(
                    zone_name=LOCK["name"], event_type=GeofenceEventType.ZONE_ENTRY, entity_uid=lead.lead_uid,
                    mmsi=IDLE_MMSI, detail="baseline backfill", detected_at=now - dt.timedelta(days=day_offset),
                )
            )
        for day_offset in range(7, 0, -1):
            for _ in range(3):  # 3x the baseline daily rate in the recent window
                db.add(
                    GeofenceEvent(
                        zone_name=LOCK["name"], event_type=GeofenceEventType.ZONE_ENTRY, entity_uid=lead.lead_uid,
                        mmsi=IDLE_MMSI, detail="recent spike backfill", detected_at=now - dt.timedelta(days=day_offset, hours=1),
                    )
                )
        db.commit()
        expansion_trigger = river_surveillance.check_expansion_trigger(db, lead.lead_uid, lead.business_name, LOCK["name"])
        check("Expansion trigger fires when recent dock-visit rate exceeds baseline x multiplier", expansion_trigger is not None)
        db.refresh(lead)
        check("Expansion trigger also appends an audit-trailed note", "expansion_throughput_spike" in (lead.notes or ""))

        no_baseline_entity = uuid.uuid4()
        no_signal = river_surveillance.check_expansion_trigger(db, no_baseline_entity, "No Baseline Co", LOCK["name"])
        check("Expansion trigger does NOT fabricate a signal with zero baseline history", no_signal is None)

        # --- Full sweep, end to end ---
        reset_db(db)
        db2_lead = pipeline.create_lead(db, business_name="Verify River Grain Co", co_broker=CoBroker.NICK_F)
        summary = river_surveillance.run_full_sweep(db)
        check(
            "run_full_sweep returns a complete summary dict",
            all(k in summary for k in ("lock_status_events", "lock_queue_events", "velocity_anomaly_events", "zone_entry_events", "distress_triggers", "expansion_triggers", "friction_metrics_computed", "zones_monitored")),
            str(summary),
        )
        check("run_full_sweep monitors all configured zones", summary["zones_monitored"] == len(river_surveillance.WATERWAY_ZONES))
        print(f"\nrun_full_sweep summary: {summary}")

    reset_db(db)
    db.query(MasterLogEntry).filter(MasterLogEntry.business_name.like("Verify River%")).delete(synchronize_session=False)
    db.commit()
    db.close()


async def verify_nonblocking_loop() -> None:
    """Proves the background polling loop doesn't block the event loop:
    runs a heartbeat coroutine that increments a counter every 20ms
    concurrently with a sweep cycle (using a mocked, fast HTTP transport
    and a near-zero poll interval), and asserts the heartbeat kept
    ticking throughout -- if run_full_sweep were blocking the loop
    directly instead of running inside asyncio.to_thread, the heartbeat
    would stall for the duration of the sweep."""
    heartbeat_ticks = 0
    stop = False

    async def heartbeat():
        nonlocal heartbeat_ticks
        while not stop:
            heartbeat_ticks += 1
            await asyncio.sleep(0.02)

    from app.config import get_settings

    get_settings().river_surveillance_poll_interval_seconds = 100  # don't actually loop again during this test

    with mock.patch("httpx.Client", mock_client_factory):
        heartbeat_task = asyncio.create_task(heartbeat())
        start = time.monotonic()
        task = river_surveillance.start_background_polling()
        check("Background polling task starts when RIVER_SURVEILLANCE_ENABLED=true", task is not None)
        await asyncio.sleep(1.5)  # let at least one sweep cycle run inside asyncio.to_thread
        elapsed = time.monotonic() - start
        river_surveillance.stop_background_polling()
        stop = True
        await asyncio.sleep(0.05)
        heartbeat_task.cancel()

    expected_min_ticks = int(elapsed / 0.02 * 0.5)  # tolerant lower bound
    check(
        "Event loop heartbeat kept ticking while a sweep ran in the background (event loop not blocked)",
        heartbeat_ticks >= expected_min_ticks,
        f"{heartbeat_ticks} ticks in {elapsed:.2f}s (expected >= {expected_min_ticks})",
    )


if __name__ == "__main__":
    main()
    asyncio.run(verify_nonblocking_loop())

    print(f"\n{len(results)}/{len(results)} checks passed." if all(r[1] for r in results) else "SOME CHECKS FAILED.")
