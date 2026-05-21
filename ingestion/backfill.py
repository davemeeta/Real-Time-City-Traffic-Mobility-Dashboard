"""
backfill.py
-----------
Generates 14 days of synthetic historical sensor readings and writes them
directly to TimescaleDB. Run this ONCE after docker-compose up to give
your Prophet / LSTM model enough data to train on immediately.

Run:
    python backfill.py
"""

import random
import logging
import math
from datetime import datetime, timezone, timedelta

import psycopg2
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     "localhost",
    "port":     5433,
    "dbname":   "traffic_db",
    "user":     "traffic_user",
    "password": "traffic_pass",
}

SENSORS = [
    {"id": "STR_B14_001", "city": "stuttgart", "type": "primary",  "cap": 1800},
    {"id": "STR_B14_002", "city": "stuttgart", "type": "primary",  "cap": 1800},
    {"id": "STR_A8_001",  "city": "stuttgart", "type": "motorway", "cap": 3600},
    {"id": "STR_A8_002",  "city": "stuttgart", "type": "motorway", "cap": 3600},
    {"id": "STR_B10_001", "city": "stuttgart", "type": "primary",  "cap": 1600},
    {"id": "STR_B27_001", "city": "stuttgart", "type": "primary",  "cap": 1600},
    {"id": "MUC_A9_001",  "city": "munich",    "type": "motorway", "cap": 4000},
    {"id": "MUC_A9_002",  "city": "munich",    "type": "motorway", "cap": 4000},
    {"id": "MUC_B2R_001", "city": "munich",    "type": "primary",  "cap": 2000},
    {"id": "MUC_B2R_002", "city": "munich",    "type": "primary",  "cap": 2000},
    {"id": "MUC_A96_001", "city": "munich",    "type": "motorway", "cap": 3600},
    {"id": "MUC_B13_001", "city": "munich",    "type": "primary",  "cap": 1400},
]

DAYS_BACK    = 14
INTERVAL_MIN = 5           # reading every 5 minutes


def rush_factor(hour: int, weekday: int, sensor_id: str) -> float:
    """Deterministic rush-hour model — same as producer.py but seeded for history."""
    rng = random.Random(sensor_id + str(hour) + str(weekday))
    if weekday >= 5:
        if 10 <= hour <= 14:
            return rng.uniform(0.35, 0.55)
        return rng.uniform(0.10, 0.25)

    if hour in (7, 8):
        return rng.uniform(0.70, 0.93)
    if hour in (9,):
        return rng.uniform(0.48, 0.68)
    if hour in (12, 13):
        return rng.uniform(0.38, 0.55)
    if hour in (16, 17, 18):
        return rng.uniform(0.75, 0.96)
    if hour in (19,):
        return rng.uniform(0.38, 0.60)
    if 0 <= hour <= 5:
        return rng.uniform(0.02, 0.08)
    return rng.uniform(0.15, 0.38)


def weather_for_day(day: datetime) -> dict:
    """Pseudorandom weather (no API call for backfill)."""
    rng = random.Random(day.strftime("%Y-%m-%d"))
    return {
        "temp": rng.uniform(-5, 28),
        "rain": rng.choices([0.0, rng.uniform(0.5, 8.0)], weights=[0.7, 0.3])[0],
    }


def build_rows(sensor: dict, start: datetime, end: datetime) -> list[tuple]:
    rows   = []
    cursor = start
    rng    = random.Random(sensor["id"])

    while cursor <= end:
        w       = weather_for_day(cursor)
        hour    = cursor.hour
        weekday = cursor.weekday()

        base       = rush_factor(hour, weekday, sensor["id"])
        rain_pen   = 0.12 if w["rain"] > 2 else (0.06 if w["rain"] > 0.5 else 0.0)
        cold_pen   = 0.08 if w["temp"] < 2 else 0.0
        sensor_b   = rng.uniform(-0.05, 0.08)
        congestion = max(0.0, min(1.0, base + rain_pen + cold_pen + sensor_b + rng.gauss(0, 0.03)))

        if sensor["type"] == "motorway":
            congestion = congestion * 0.85
        else:
            congestion = min(congestion * 1.1, 1.0)

        free_flow = 130 if sensor["type"] == "motorway" else 70
        speed     = max(5.0, free_flow * (1 - congestion * 0.85) + rng.gauss(0, 2))
        volume    = int(sensor["cap"] / 12 * congestion * rng.uniform(0.85, 1.15))
        occupancy = max(0.0, min(1.0, congestion * 0.6 + rng.gauss(0, 0.02)))

        rows.append((
            cursor,
            sensor["id"],
            round(speed, 1),
            volume,
            round(occupancy, 4),
            round(congestion, 4),
            round(w["temp"], 1),
            round(w["rain"], 2),
        ))
        cursor += timedelta(minutes=INTERVAL_MIN)

    return rows


def main():
    conn   = psycopg2.connect(**DB_CONFIG)
    cursor = conn.cursor()

    now   = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(days=DAYS_BACK)

    total_rows = 0
    for sensor in SENSORS:
        log.info(f"Backfilling {sensor['id']} — {DAYS_BACK} days × {INTERVAL_MIN} min intervals…")
        rows = build_rows(sensor, start, now)

        execute_values(
            cursor,
            """
            INSERT INTO sensor_readings
                (time, sensor_id, speed_avg, volume, occupancy, congestion, weather_temp, weather_rain)
            VALUES %s
            ON CONFLICT DO NOTHING
            """,
            rows,
            page_size=1000,
        )
        conn.commit()
        total_rows += len(rows)
        log.info(f"  ✓ {len(rows):,} rows written for {sensor['id']}")

    log.info(f"\nBackfill complete. Total rows written: {total_rows:,}")
    log.info("Your TimescaleDB is ready for ML training.")
    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()
    