"""
verify.py
---------
Run after backfill.py to confirm your TimescaleDB contains data.
Shows row counts, time range, and a sample of recent readings.

Run:
    python verify.py
"""

import psycopg2

DB_CONFIG = {
    "host":     "localhost",
    "port":     5433,
    "dbname":   "traffic_db",
    "user":     "traffic_user",
    "password": "traffic_pass",
}

def run(conn, query, label):
    cur = conn.cursor()
    cur.execute(query)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")
    print("  " + "  |  ".join(f"{c:<18}" for c in cols))
    print("  " + "-" * (len(cols) * 22))
    for row in rows:
        print("  " + "  |  ".join(f"{str(v):<18}" for v in row))

def main():
    conn = psycopg2.connect(**DB_CONFIG)

    run(conn, "SELECT COUNT(*) AS total_rows FROM sensor_readings;",
        "Total rows in sensor_readings")

    run(conn, """
        SELECT sensor_id,
               COUNT(*) AS rows,
               MIN(time)::text AS earliest,
               MAX(time)::text AS latest
        FROM sensor_readings
        GROUP BY sensor_id
        ORDER BY sensor_id;
    """, "Per-sensor row counts and time range")

    run(conn, """
        SELECT time::text, sensor_id, speed_avg, volume, congestion
        FROM sensor_readings
        ORDER BY time DESC
        LIMIT 6;
    """, "6 most recent readings")

    run(conn, "SELECT COUNT(*) AS incident_count FROM incidents;",
        "Incidents fetched from MobiData BW")

    conn.close()
    print("\n✓ Verification complete. If rows > 0, your pipeline is working!\n")

if __name__ == "__main__":
    main()
