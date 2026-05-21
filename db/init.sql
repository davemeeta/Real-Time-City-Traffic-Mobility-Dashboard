-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ─────────────────────────────────────────────
-- Sensor metadata (static — one row per sensor)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sensors (
    sensor_id     TEXT PRIMARY KEY,
    road_name     TEXT NOT NULL,
    city          TEXT NOT NULL,          -- 'stuttgart' | 'munich'
    latitude      DOUBLE PRECISION NOT NULL,
    longitude     DOUBLE PRECISION NOT NULL,
    road_type     TEXT,                   -- 'motorway' | 'primary' | 'secondary'
    capacity_vph  INT DEFAULT 1800        -- vehicles per hour capacity
);

-- ─────────────────────────────────────────────
-- Time-series sensor readings (hypertable)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sensor_readings (
    time          TIMESTAMPTZ NOT NULL,
    sensor_id     TEXT NOT NULL REFERENCES sensors(sensor_id),
    speed_avg     FLOAT,                  -- km/h
    volume        INT,                    -- vehicles per 5 min
    occupancy     FLOAT,                  -- 0.0–1.0 (% time loop is occupied)
    congestion    FLOAT,                  -- derived: 0.0 (free) → 1.0 (jam)
    weather_temp  FLOAT,                  -- °C from Open-Meteo
    weather_rain  FLOAT                   -- mm/h from Open-Meteo
);

-- Convert to hypertable, partition by day
SELECT create_hypertable(
    'sensor_readings', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

ALTER TABLE sensor_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'sensor_id',  -- Groups all data for a specific sensor together
    timescaledb.compress_orderby = 'time DESC'      -- Sorts the metrics chronologically inside the chunk
);
-- Compress chunks older than 7 days
SELECT add_compression_policy('sensor_readings', INTERVAL '7 days');

-- Useful index for per-sensor queries
CREATE INDEX IF NOT EXISTS idx_readings_sensor_time
    ON sensor_readings (sensor_id, time DESC);

-- ─────────────────────────────────────────────
-- Traffic incidents (from MobiData BW real API)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS incidents (
    incident_id   TEXT PRIMARY KEY,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    city          TEXT,
    description   TEXT,
    latitude      DOUBLE PRECISION,
    longitude     DOUBLE PRECISION,
    severity      TEXT,                   -- 'low' | 'medium' | 'high'
    road_name     TEXT,
    raw_xml       TEXT                    -- store original TIC3 payload
);

-- ─────────────────────────────────────────────
-- Forecast cache (written by ML service)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS forecasts (
    sensor_id     TEXT NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    horizon_min   INT NOT NULL,           -- 30 or 60
    forecast_time TIMESTAMPTZ NOT NULL,
    congestion_pred FLOAT NOT NULL,
    congestion_lo   FLOAT,               -- lower confidence bound
    congestion_hi   FLOAT,               -- upper confidence bound
    model_name    TEXT DEFAULT 'prophet',
    PRIMARY KEY (sensor_id, horizon_min, forecast_time)
);

-- ─────────────────────────────────────────────
-- Seed sensor locations (Stuttgart + Munich)
-- Real GPS coords on major arterials
-- ─────────────────────────────────────────────
INSERT INTO sensors VALUES
  ('STR_B14_001', 'B14 Cannstatter Str',  'stuttgart', 48.7928, 9.2350, 'primary',  1800),
  ('STR_B14_002', 'B14 König-Karl-Brücke','stuttgart', 48.7882, 9.2402, 'primary',  1800),
  ('STR_A8_001',  'A8 Stuttgart-Degerloch','stuttgart',48.7341, 9.2031, 'motorway', 3600),
  ('STR_A8_002',  'A8 Richtung München',  'stuttgart', 48.7298, 9.2154, 'motorway', 3600),
  ('STR_B10_001', 'B10 Heilbronner Str',  'stuttgart', 48.8012, 9.1891, 'primary',  1600),
  ('STR_B27_001', 'B27 Pragstraße',       'stuttgart', 48.8156, 9.1923, 'primary',  1600),
  ('MUC_A9_001',  'A9 München-Nord',      'munich',    48.2147, 11.6012,'motorway', 4000),
  ('MUC_A9_002',  'A9 München-Schwabing', 'munich',    48.1812, 11.5823,'motorway', 4000),
  ('MUC_B2R_001', 'B2R Mittlerer Ring W', 'munich',    48.1391, 11.5234,'primary',  2000),
  ('MUC_B2R_002', 'B2R Mittlerer Ring N', 'munich',    48.1623, 11.5567,'primary',  2000),
  ('MUC_A96_001', 'A96 München-West',     'munich',    48.1234, 11.4521,'motorway', 3600),
  ('MUC_B13_001', 'B13 Leopoldstr',       'munich',    48.1789, 11.5712,'primary',  1400)
ON CONFLICT (sensor_id) DO NOTHING;

COMMENT ON TABLE sensor_readings IS
  'Main hypertable. Partitioned daily. Compressed after 7 days.';
COMMENT ON TABLE incidents IS
  'Raw incidents from MobiData BW TIC3 feed, fetched every 10 min.';
  