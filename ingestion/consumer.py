"""
consumer.py
-----------
Reads from Kafka topics and writes to TimescaleDB.

Run alongside producer.py:
    python consumer.py
"""

import json
import logging
import psycopg2
from datetime import datetime, timezone
from kafka import KafkaConsumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

KAFKA_BOOTSTRAP = "localhost:9092"
TOPICS          = ["traffic.sensors", "traffic.incidents"]

DB_CONFIG = {
    "host":     "localhost",
    "port":     5433,
    "dbname":   "traffic_db",
    "user":     "traffic_user",
    "password": "traffic_pass",
}


def get_db_conn():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


INSERT_READING = """
    INSERT INTO sensor_readings
        (time, sensor_id, speed_avg, volume, occupancy, congestion, weather_temp, weather_rain)
    VALUES
        (%(time)s, %(sensor_id)s, %(speed_avg)s, %(volume)s,
         %(occupancy)s, %(congestion)s, %(weather_temp)s, %(weather_rain)s)
    ON CONFLICT DO NOTHING;
"""

INSERT_INCIDENT = """
    INSERT INTO incidents
        (incident_id, fetched_at, city, description, latitude, longitude, raw_xml)
    VALUES
        (%(incident_id)s, %(fetched_at)s, %(city)s, %(description)s,
         %(latitude)s, %(longitude)s, %(raw_xml)s)
    ON CONFLICT (incident_id) DO NOTHING;
"""


def handle_reading(cursor, payload: dict):
    cursor.execute(INSERT_READING, {
        "time":         payload["timestamp"],
        "sensor_id":    payload["sensor_id"],
        "speed_avg":    payload.get("speed_avg"),
        "volume":       payload.get("volume"),
        "occupancy":    payload.get("occupancy"),
        "congestion":   payload.get("congestion"),
        "weather_temp": payload.get("weather_temp"),
        "weather_rain": payload.get("weather_rain"),
    })


def handle_incident(cursor, payload: dict):
    cursor.execute(INSERT_INCIDENT, {
        "incident_id": payload.get("incident_id", "unknown"),
        "fetched_at":  datetime.now(tz=timezone.utc).isoformat(),
        "city":        payload.get("city"),
        "description": payload.get("description"),
        "latitude":    payload.get("latitude"),
        "longitude":   payload.get("longitude"),
        "raw_xml":     json.dumps(payload),
    })


def main():
    conn   = get_db_conn()
    cursor = conn.cursor()

    consumer = KafkaConsumer(
        *TOPICS,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id="traffic-db-writer",
        auto_offset_reset="latest",
        enable_auto_commit=True,
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        consumer_timeout_ms=60_000,
    )
    log.info(f"Consumer subscribed to {TOPICS}. Writing to TimescaleDB…")

    batch_count = 0
    try:
        for msg in consumer:
            topic   = msg.topic
            payload = msg.value
            try:
                if topic == "traffic.sensors":
                    handle_reading(cursor, payload)
                elif topic == "traffic.incidents":
                    handle_incident(cursor, payload)

                batch_count += 1
                if batch_count % 20 == 0:          # commit every 20 rows
                    conn.commit()
                    log.info(f"Committed {batch_count} rows so far…")

            except Exception as e:
                log.error(f"Row insert failed ({topic}): {e} — payload: {payload}")
                conn.rollback()

    except KeyboardInterrupt:
        log.info("Shutting down consumer.")
    finally:
        conn.commit()
        cursor.close()
        conn.close()
        consumer.close()
        log.info("Consumer closed cleanly.")


if __name__ == "__main__":
    main()
