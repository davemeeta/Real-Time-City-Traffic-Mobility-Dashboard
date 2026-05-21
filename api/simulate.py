"""
simulate.py  —  Bureau of Public Roads (BPR) simulation engine

When a road sensor is "closed", its traffic volume gets redistributed
to nearby sensors weighted by inverse distance. The BPR function then
recalculates travel time / congestion for each affected sensor.

BPR formula:
    t = t0 * (1 + alpha * (v / c) ^ beta)

Where:
    t0    = free-flow travel time (proxy: 1.0)
    v     = volume (vehicles/period)
    c     = capacity
    alpha = 0.15  (standard BPR constant)
    beta  = 4     (standard BPR constant)

Congestion is derived as: min(v / c, 1.0)
"""

import math
from typing import Any

ALPHA = 0.15
BETA  = 4
MAX_REDISTRIBUTION_KM = 3.0     # only redistribute to sensors within 3 km


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Straight-line distance between two GPS coords in km."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bpr_congestion(volume: float, capacity: int) -> float:
    """Returns 0.0–1.0 congestion score via BPR function."""
    if capacity <= 0:
        return 0.0
    ratio = volume / capacity
    bpr   = 1 + ALPHA * (ratio ** BETA)
    # Normalise: bpr=1 → congestion=0, bpr=2.15 (v=c) → congestion=1
    return min(max((bpr - 1) / (ALPHA), 0.0), 1.0)


def run_simulation(
    sensors: list[dict[str, Any]],
    closed_ids: list[str],
) -> list[dict[str, Any]]:
    """
    Args:
        sensors:    list of sensor dicts from DB (sensor_id, lat, lon, capacity_vph, congestion, volume)
        closed_ids: sensor_ids to mark as closed

    Returns:
        List of all sensors with updated congestion scores and a `changed` flag.
    """
    closed_set = set(closed_ids)

    # Build working copy with mutable volumes
    state = {
        s["sensor_id"]: {
            **s,
            "volume":        s.get("volume") or 0,
            "congestion":    s.get("congestion") or 0.0,
            "new_volume":    s.get("volume") or 0,
            "new_congestion":s.get("congestion") or 0.0,
            "closed":        s["sensor_id"] in closed_set,
            "changed":       False,
        }
        for s in sensors
    }

    # For each closed sensor, redistribute its volume to neighbours
    for sid in closed_ids:
        if sid not in state:
            continue
        closed = state[sid]
        diverted_volume = closed["volume"]

        if diverted_volume <= 0:
            continue

        # Find open neighbours within radius
        neighbours = []
        for nid, n in state.items():
            if nid in closed_set or n["latitude"] is None:
                continue
            dist = _haversine_km(
                closed["latitude"], closed["longitude"],
                n["latitude"],      n["longitude"],
            )
            if dist <= MAX_REDISTRIBUTION_KM and dist > 0:
                neighbours.append((nid, dist))

        if not neighbours:
            continue

        # Weight by inverse distance — closer roads absorb more traffic
        total_inv_dist = sum(1 / d for _, d in neighbours)
        for nid, dist in neighbours:
            weight = (1 / dist) / total_inv_dist
            state[nid]["new_volume"] += int(diverted_volume * weight)

        # Closed sensor drops to 0
        state[sid]["new_volume"]     = 0
        state[sid]["new_congestion"] = 0.0
        state[sid]["changed"]        = True

    # Recalculate congestion for all sensors that received extra volume
    for sid, s in state.items():
        if sid in closed_set:
            continue
        cap = s.get("capacity_vph") or 1800
        new_cong = _bpr_congestion(s["new_volume"], cap)
        if abs(new_cong - (s["congestion"] or 0.0)) > 0.01:
            s["changed"] = True
        s["new_congestion"] = round(new_cong, 4)

    # Return clean output
    return [
        {
            "sensor_id":      s["sensor_id"],
            "road_name":      s.get("road_name"),
            "city":           s.get("city"),
            "latitude":       s.get("latitude"),
            "longitude":      s.get("longitude"),
            "closed":         s["closed"],
            "changed":        s["changed"],
            "congestion_before": round(s["congestion"] or 0.0, 4),
            "congestion_after":  s["new_congestion"],
            "volume_before":  s["volume"],
            "volume_after":   s["new_volume"],
        }
        for s in state.values()
    ]