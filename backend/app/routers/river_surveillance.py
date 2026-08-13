from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.orm import GeofenceEvent, WaterwayFrictionMetric, WaterwayTelemetrySnapshot
from app.services import river_surveillance

router = APIRouter(prefix="/api/river-surveillance", tags=["river-surveillance"])


@router.get("/zones")
def list_zones():
    """Static reference: every monitored waterway zone (locks, Gulf
    ports, the Seaway) -- see river_surveillance.py::WATERWAY_ZONES."""
    return river_surveillance.WATERWAY_ZONES


@router.get("/vessels")
def list_vessels(db: Session = Depends(get_db)):
    """Latest known position per tracked vessel (by MMSI), classified
    green/amber/red for the globe overlay -- see
    river_surveillance.py::classify_vessel_state for what each state
    means."""
    latest_per_mmsi = (
        db.query(
            WaterwayTelemetrySnapshot.mmsi,
            func.max(WaterwayTelemetrySnapshot.observed_at).label("latest_observed_at"),
        )
        .filter(WaterwayTelemetrySnapshot.mmsi.isnot(None))
        .group_by(WaterwayTelemetrySnapshot.mmsi)
        .subquery()
    )
    latest_snapshots = (
        db.query(WaterwayTelemetrySnapshot)
        .join(
            latest_per_mmsi,
            (WaterwayTelemetrySnapshot.mmsi == latest_per_mmsi.c.mmsi)
            & (WaterwayTelemetrySnapshot.observed_at == latest_per_mmsi.c.latest_observed_at),
        )
        .all()
    )

    results = []
    for snap in latest_snapshots:
        if snap.latitude is None or snap.longitude is None:
            continue
        speed = float(snap.speed_knots) if snap.speed_knots is not None else None
        state = river_surveillance.classify_vessel_state(db, snap.mmsi, snap.zone_name, speed)

        # Powers the Funded-lead "crown jewel ... links to active
        # supply-chain stream" visual on the globe -- the most recent
        # cargo-cross-reference match (if any) for this vessel's MMSI.
        linked_event = (
            db.query(GeofenceEvent)
            .filter(GeofenceEvent.mmsi == snap.mmsi, GeofenceEvent.entity_uid.isnot(None))
            .order_by(GeofenceEvent.detected_at.desc())
            .first()
        )

        results.append(
            {
                "mmsi": snap.mmsi,
                "imo": snap.imo,
                "vessel_name": snap.vessel_name,
                "zone_name": snap.zone_name,
                "latitude": float(snap.latitude),
                "longitude": float(snap.longitude),
                "speed_knots": speed,
                "state": state,
                "observed_at": snap.observed_at.isoformat(),
                "linked_lead_uid": str(linked_event.entity_uid) if linked_event else None,
            }
        )
    return results


@router.get("/friction")
def list_friction(db: Session = Depends(get_db)):
    """Latest computed friction score per monitored zone -- see
    river_surveillance.py::compute_friction_index."""
    results = []
    for zone in river_surveillance.WATERWAY_ZONES:
        latest = (
            db.query(WaterwayFrictionMetric)
            .filter(WaterwayFrictionMetric.zone_name == zone["name"])
            .order_by(WaterwayFrictionMetric.computed_at.desc())
            .first()
        )
        if latest is None:
            continue
        results.append(
            {
                "zone_name": zone["name"],
                "latitude": zone["lat"],
                "longitude": zone["lon"],
                "friction_score": float(latest.friction_score),
                "active_queue_count": latest.active_queue_count,
                "velocity_anomaly_count": latest.velocity_anomaly_count,
                "computed_at": latest.computed_at.isoformat(),
            }
        )
    return results
