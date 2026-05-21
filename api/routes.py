"""
routes.py  —  REST endpoints

GET  /api/sensors                          — all sensors + latest reading
GET  /api/sensors/{id}/history             — time-series (query params: from, to, limit)
GET  /api/forecast/{id}                    — cached 30 & 60 min forecast from DB
POST /api/simulate                         — what-if road closure simulation
GET  /api/incidents                        — latest incidents from MobiData BW
"""

from fastapi import APIRouter, HTTPException, Query
from datetime import datetime, timezone, timedelta
from typing import Optional
from pydantic import BaseModel

from db import get_pool
from simulate import run_simulation

router = APIRouter()


# ── /api/sensors ─────────────────────────────────────────────────────────────

@router.get("/sensors")
async def list_sensors():
    """All sensor metadata joined with their most recent reading."""
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT
            s.sensor_id,
            s.road_name,
            s.city,
            s.latitude,
            s.longitude,
            s.road_type,
            s.capacity_vph,
            r.speed_avg,
            r.volume,
            r.occupancy,
            r.congestion,
            r.weather_temp,
            r.weather_rain,
            r.time AS last_updated
        FROM sensors s
        LEFT JOIN LATERAL (
            SELECT * FROM sensor_readings
            WHERE sensor_id = s.sensor_id
            ORDER BY time DESC
            LIMIT 1
        ) r ON true
        ORDER BY s.city, s.road_name
    """)
    return [dict(r) for r in rows]


# ── /api/sensors/{id}/history ─────────────────────────────────────────────────

@router.get("/sensors/{sensor_id}/history")
async def sensor_history(
    sensor_id: str,
    start: Optional[str] = Query(None, description="ISO8601 start time"),
    end:   Optional[str] = Query(None, description="ISO8601 end time"),
    limit: int           = Query(288, ge=1, le=2000, description="Max rows (default=288 = 24h at 5min)"),
):
    """
    Time-series readings for one sensor.
    Defaults to the last 24 hours if no start/end given.
    """
    pool = get_pool()
    now  = datetime.now(tz=timezone.utc)

    try:
        t_end   = datetime.fromisoformat(end)   if end   else now
        t_start = datetime.fromisoformat(start) if start else now - timedelta(hours=24)
    except ValueError:
        raise HTTPException(400, "Invalid datetime format. Use ISO8601 e.g. 2024-05-01T08:00:00Z")

    rows = await pool.fetch("""
        SELECT time, speed_avg, volume, occupancy, congestion, weather_temp, weather_rain
        FROM sensor_readings
        WHERE sensor_id = $1
          AND time BETWEEN $2 AND $3
        ORDER BY time DESC
        LIMIT $4
    """, sensor_id, t_start, t_end, limit)

    if not rows:
        raise HTTPException(404, f"No data found for sensor '{sensor_id}'")

    return {
        "sensor_id": sensor_id,
        "start":     t_start.isoformat(),
        "end":       t_end.isoformat(),
        "count":     len(rows),
        "readings":  [dict(r) for r in rows],
    }


# ── /api/forecast/{id} ────────────────────────────────────────────────────────

@router.get("/forecast/{sensor_id}")
async def get_forecast(sensor_id: str):
    """
    Returns the latest 30-min and 60-min forecasts from the forecasts table.
    The ML service (ml/train_prophet.py) writes these after each training run.
    """
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT horizon_min, forecast_time, congestion_pred,
               congestion_lo, congestion_hi, model_name, generated_at
        FROM forecasts
        WHERE sensor_id = $1
          AND generated_at >= NOW() - INTERVAL '2 hours'
        ORDER BY horizon_min, forecast_time
    """, sensor_id)

    if not rows:
        raise HTTPException(404, f"No forecast available for '{sensor_id}'. Run ml/train_prophet.py first.")

    return {
        "sensor_id": sensor_id,
        "forecasts": [dict(r) for r in rows],
    }


# ── /api/simulate ─────────────────────────────────────────────────────────────

class SimulateRequest(BaseModel):
    closed_sensor_ids: list[str]
    description: str = ""


@router.post("/simulate")
async def simulate(body: SimulateRequest):
    """
    What-if simulation: close one or more road sensors and see how
    congestion ripples to neighbouring sensors via the BPR function.

    Returns updated congestion scores for all affected sensors.
    """
    pool = get_pool()

    # Fetch current readings for all sensors
    rows = await pool.fetch("""
        SELECT s.sensor_id, s.road_type, s.capacity_vph,
               s.latitude, s.longitude, r.congestion, r.volume
        FROM sensors s
        LEFT JOIN LATERAL (
            SELECT congestion, volume FROM sensor_readings
            WHERE sensor_id = s.sensor_id
            ORDER BY time DESC LIMIT 1
        ) r ON true
    """)

    current_state = [dict(r) for r in rows]
    result = run_simulation(current_state, body.closed_sensor_ids)

    return {
        "closed_sensors": body.closed_sensor_ids,
        "affected_count": len([r for r in result if r["changed"]]),
        "sensors":        result,
    }


# ── /api/incidents ────────────────────────────────────────────────────────────

@router.get("/incidents")
async def list_incidents(limit: int = Query(50, ge=1, le=500)):
    """Latest traffic incidents fetched from MobiData BW."""
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT incident_id, fetched_at, city, description,
               latitude, longitude, severity, road_name
        FROM incidents
        ORDER BY fetched_at DESC
        LIMIT $1
    """, limit)
    return [dict(r) for r in rows]
