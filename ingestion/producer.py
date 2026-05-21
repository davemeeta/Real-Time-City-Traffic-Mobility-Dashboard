"""
producer.py
-----------
Kafka producer for the traffic dashboard.

Data sources (all FREE, no credit card):
  1. MobiData BW  — real traffic incidents for Stuttgart/BW, no API key needed
  2. Open-Meteo   — real weather (temp, rain), no API key ever
  3. Synthetic    — realistic loop-detector readings seeded from real coords + time patterns

Run:
    python producer.py
"""

import json
import math
import random
import time
import logging
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from kafka import KafkaProducer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Kafka config ──────────────────────────────────────────────────────────────
KAFKA_BOOTSTRAP = "localhost:9092"
TOPIC_READINGS  = "traffic.sensors"
TOPIC_INCIDENTS = "traffic.incidents"
POLL_INTERVAL   = 60          # seconds between full poll cycles

# ── Sensor registry (must match db/init.sql seeds) ───────────────────────────
SENSORS = [
    {"id": "STR_B14_001", "lat": 48.7928, "lon": 9.2350,  "city": "stuttgart", "road": "B14 Cannstatter Str",   "type": "primary",  "cap": 1800},
    {"id": "STR_B14_002", "lat": 48.7882, "lon": 9.2402,  "city": "stuttgart", "road": "B14 König-Karl-Brücke","type": "primary",  "cap": 1800},
    {"id": "STR_A8_001",  "lat": 48.7341, "lon": 9.2031,  "city": "stuttgart", "road": "A8 Stuttgart-Degerloch","type":"motorway", "cap": 3600},
    {"id": "STR_A8_002",  "lat": 48.7298, "lon": 9.2154,  "city": "stuttgart", "road": "A8 Richtung München",  "type": "motorway", "cap": 3600},
    {"id": "STR_B10_001", "lat": 48.8012, "lon": 9.1891,  "city": "stuttgart", "road": "B10 Heilbronner Str",  "type": "primary",  "cap": 1600},
    {"id": "STR_B27_001", "lat": 48.8156, "lon": 9.1923,  "city": "stuttgart", "road": "B27 Pragstraße",       "type": "primary",  "cap": 1600},
    {"id": "MUC_A9_001",  "lat": 48.2147, "lon": 11.6012, "city": "munich",    "road": "A9 München-Nord",      "type": "motorway", "cap": 4000},
    {"id": "MUC_A9_002",  "lat": 48.1812, "lon": 11.5823, "city": "munich",    "road": "A9 München-Schwabing", "type": "motorway", "cap": 4000},
    {"id": "MUC_B2R_001", "lat": 48.1391, "lon": 11.5234, "city": "munich",    "road": "B2R Mittlerer Ring W", "type": "primary",  "cap": 2000},
    {"id": "MUC_B2R_002", "lat": 48.1623, "lon": 11.5567, "city": "munich",    "road": "B2R Mittlerer Ring N", "type": "primary",  "cap": 2000},
    {"id": "MUC_A96_001", "lat": 48.1234, "lon": 11.4521, "city": "munich",    "road": "A96 München-West",     "type": "motorway", "cap": 3600},
    {"id": "MUC_B13_001", "lat": 48.1789, "lon": 11.5712, "city": "munich",    "road": "B13 Leopoldstr",       "type": "primary",  "cap": 1400},
]

# ── Weather cache (shared across all sensors in same city) ───────────────────
_weather_cache: dict[str, dict] = {}
_weather_fetched_at: float = 0

def fetch_weather() -> dict[str, dict]:
    """
    Fetch real weather for Stuttgart and Munich from Open-Meteo.
    Completely free — no API key, no credit card.
    Returns {"stuttgart": {"temp": 12.3, "rain": 0.0}, "munich": {...}}
    """
    global _weather_cache, _weather_fetched_at
    now = time.time()
    if now - _weather_fetched_at < 600:       # cache 10 min
        return _weather_cache

    coords = {
        "stuttgart": (48.7758, 9.1829),
        "munich":    (48.1351, 11.5820),
    }
    result = {}
    for city, (lat, lon) in coords.items():
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,precipitation"
            f"&timezone=Europe/Berlin"
        )
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            cur = r.json()["current"]
            result[city] = {
                "temp": cur.get("temperature_2m", 10.0),
                "rain": cur.get("precipitation", 0.0),
            }
        except Exception as e:
            log.warning(f"Weather fetch failed for {city}: {e}")
            result[city] = {"temp": 10.0, "rain": 0.0}

    _weather_cache = result
    _weather_fetched_at = now
    log.info(f"Weather updated: {result}")
    return result


def fetch_incidents_bw() -> list[dict]:
    """
    Fetch real traffic incidents from MobiData BW TIC3 feed.
    No API key needed. Updated every 10 minutes by the platform.
    """
    url = "https://api.mobidata-bw.de/datasets/traffic/incidents-bw/TIC3-Meldungen.xml"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.warning(f"Incident fetch failed: {e}")
        return []

    incidents = []
    # TIC3 is a German XML format — we extract the key fields
    ns = {"tic": "http://datex2.eu/schema/2/2_0"}
    for situation in root.iter():
        tag = situation.tag.split("}")[-1] if "}" in situation.tag else situation.tag
        if tag in ("Meldung", "situation", "Situation"):
            incident = {}
            for child in situation.iter():
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "Meldungstext" and child.text:
                    incident["description"] = child.text.strip()
                elif ctag in ("latitude", "lat") and child.text:
                    try:
                        incident["latitude"] = float(child.text)
                    except ValueError:
                        pass
                elif ctag in ("longitude", "lon") and child.text:
                    try:
                        incident["longitude"] = float(child.text)
                    except ValueError:
                        pass
            if incident:
                # Generate stable ID from content hash
                h = hashlib.md5(json.dumps(incident, sort_keys=True).encode()).hexdigest()[:12]
                incident["incident_id"] = f"TIC3_{h}"
                incident["city"] = "stuttgart"
                incidents.append(incident)

    log.info(f"Fetched {len(incidents)} incidents from MobiData BW")
    return incidents


# ── Realistic traffic generation ─────────────────────────────────────────────

def _rush_factor(hour: int, weekday: int) -> float:
    """
    Returns a 0–1 congestion multiplier based on hour and weekday.
    Models typical German city traffic patterns.
    """
    if weekday >= 5:                        # weekend — lighter traffic
        if 10 <= hour <= 14:
            return 0.45
        return 0.2

    # Weekday rush hours (German pattern)
    if hour in (7, 8):                      # morning peak
        return random.uniform(0.72, 0.92)
    if hour in (9,):
        return random.uniform(0.50, 0.68)
    if hour in (12, 13):                    # lunch bump
        return random.uniform(0.40, 0.55)
    if hour in (16, 17, 18):               # evening peak
        return random.uniform(0.78, 0.95)
    if hour in (19,):
        return random.uniform(0.40, 0.60)
    if 0 <= hour <= 5:                      # night
        return random.uniform(0.02, 0.08)
    return random.uniform(0.15, 0.35)      # off-peak


def _weather_penalty(weather: dict) -> float:
    """Rain and cold increase congestion by up to +0.25."""
    penalty = 0.0
    if weather["rain"] > 2.0:
        penalty += 0.15
    elif weather["rain"] > 0.5:
        penalty += 0.07
    if weather["temp"] < 2.0:              # ice / snow risk
        penalty += 0.10
    return min(penalty, 0.25)


def generate_sensor_reading(sensor: dict, weather: dict) -> dict:
    """
    Generate a realistic synthetic sensor reading.

    The synthetic data is seeded with:
      - Real GPS location (road type affects baseline capacity)
      - Real current time (rush hour patterns)
      - Real weather (rain/cold degrades flow)

    This gives a proper time-series that your LSTM will later learn from.
    """
    now = datetime.now(tz=timezone.utc)
    hour    = now.hour
    weekday = now.weekday()

    base_congestion = _rush_factor(hour, weekday)
    weather_penalty = _weather_penalty(weather)

    # Motorways clear faster; primary roads accumulate more
    if sensor["type"] == "motorway":
        congestion = min(base_congestion * 0.85 + weather_penalty, 1.0)
    else:
        congestion = min(base_congestion * 1.1 + weather_penalty, 1.0)

    # Add sensor-specific noise (seed by sensor ID for consistency across runs)
    rng = random.Random(sensor["id"])
    sensor_bias = rng.uniform(-0.05, 0.08)
    congestion = max(0.0, min(1.0, congestion + sensor_bias + random.gauss(0, 0.03)))

    # Derive physical measurements from congestion
    free_flow_speed = 130 if sensor["type"] == "motorway" else 70
    speed_avg  = free_flow_speed * (1 - congestion * 0.85) + random.gauss(0, 2)
    speed_avg  = max(5.0, speed_avg)

    volume = int(sensor["cap"] / 12 * congestion * random.uniform(0.85, 1.15))  # per 5 min
    occupancy = congestion * 0.6 + random.gauss(0, 0.02)
    occupancy = max(0.0, min(1.0, occupancy))

    return {
        "schema":       "sensor_reading_v1",
        "sensor_id":    sensor["id"],
        "road":         sensor["road"],
        "city":         sensor["city"],
        "latitude":     sensor["lat"],
        "longitude":    sensor["lon"],
        "timestamp":    now.isoformat(),
        "speed_avg":    round(speed_avg, 1),
        "volume":       volume,
        "occupancy":    round(occupancy, 4),
        "congestion":   round(congestion, 4),
        "weather_temp": weather.get("temp", 10.0),
        "weather_rain": weather.get("rain", 0.0),
    }


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    producer = KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8"),
        retries=5,
        acks="all",
    )
    log.info("Kafka producer connected. Starting poll loop…")

    while True:
        cycle_start = time.time()

        # 1. Fetch real weather (cached 10 min)
        weather_by_city = fetch_weather()

        # 2. Generate + publish sensor readings
        published = 0
        for sensor in SENSORS:
            weather = weather_by_city.get(sensor["city"], {"temp": 10.0, "rain": 0.0})
            reading = generate_sensor_reading(sensor, weather)
            producer.send(
                TOPIC_READINGS,
                key=sensor["id"],
                value=reading,
            )
            published += 1

        log.info(f"Published {published} sensor readings → topic '{TOPIC_READINGS}'")

        # 3. Fetch + publish real incidents (every cycle — they change every 10 min on the source)
        incidents = fetch_incidents_bw()
        for inc in incidents:
            producer.send(TOPIC_INCIDENTS, key=inc.get("incident_id", "unknown"), value=inc)
        if incidents:
            log.info(f"Published {len(incidents)} incidents → topic '{TOPIC_INCIDENTS}'")

        producer.flush()

        elapsed = time.time() - cycle_start
        sleep_for = max(0, POLL_INTERVAL - elapsed)
        log.info(f"Cycle done in {elapsed:.1f}s. Next cycle in {sleep_for:.0f}s.")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
    