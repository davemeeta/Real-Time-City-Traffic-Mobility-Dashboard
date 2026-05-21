"""
features.py
-----------
Shared feature engineering for both Prophet and LSTM models.
Loads sensor readings from TimescaleDB and returns clean DataFrames.
"""

import pandas as pd
import numpy as np
import psycopg2
import holidays
import logging

log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     "localhost",
    "port":     5433,
    "dbname":   "traffic_db",
    "user":     "traffic_user",
    "password": "traffic_pass",
}

SENSOR_IDS = [
    "STR_B14_001", "STR_B14_002", "STR_A8_001",  "STR_A8_002",
    "STR_B10_001", "STR_B27_001", "MUC_A9_001",  "MUC_A9_002",
    "MUC_B2R_001", "MUC_B2R_002", "MUC_A96_001", "MUC_B13_001",
]

# German public holidays (Baden-Württemberg + Bayern)
DE_HOLIDAYS_BW  = holidays.Germany(state="BW")
DE_HOLIDAYS_BAY = holidays.Germany(state="BY")


def load_sensor_data(sensor_id: str, days: int = 14) -> pd.DataFrame:
    """
    Load raw readings for one sensor from TimescaleDB.
    Returns DataFrame sorted by time ascending.
    """
    conn = psycopg2.connect(**DB_CONFIG)
    query = """
        SELECT time, speed_avg, volume, occupancy, congestion,
               weather_temp, weather_rain
        FROM sensor_readings
        WHERE sensor_id = %s
          AND time >= NOW() - INTERVAL '%s days'
        ORDER BY time ASC
    """
    df = pd.read_sql(query, conn, params=(sensor_id, days))
    conn.close()

    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()

    # Fill small gaps (up to 3 missing 5-min slots)
    df = df.resample("5min").mean()
    df = df.interpolate(method="time", limit=3)
    df = df.dropna()

    log.info(f"Loaded {len(df):,} rows for {sensor_id}")
    return df


def add_time_features(df: pd.DataFrame, city: str = "stuttgart") -> pd.DataFrame:
    """
    Add calendar and time features that help the model learn
    rush-hour patterns, weekends, and German public holidays.
    """
    idx = df.index.tz_convert("Europe/Berlin")

    df["hour"]       = idx.hour
    df["dayofweek"]  = idx.dayofweek          # 0=Mon … 6=Sun
    df["month"]      = idx.month
    df["is_weekend"] = (idx.dayofweek >= 5).astype(int)

    # Rush hour flags
    df["is_morning_rush"] = idx.hour.isin([7, 8, 9]).astype(int)
    df["is_evening_rush"] = idx.hour.isin([16, 17, 18]).astype(int)

    # German public holidays
    hol = DE_HOLIDAYS_BAY if city == "munich" else DE_HOLIDAYS_BW
    df["is_holiday"] = [
        1 if d.date() in hol else 0
        for d in idx
    ]

    # Cyclical encoding of hour (so model sees 23→0 as continuous)
    df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)
    df["dow_sin"]  = np.sin(2 * np.pi * df["dayofweek"] / 7)
    df["dow_cos"]  = np.cos(2 * np.pi * df["dayofweek"] / 7)

    # Lag features (what congestion was 30 and 60 min ago)
    df["lag_30min"] = df["congestion"].shift(6)
    df["lag_60min"] = df["congestion"].shift(12)
    df["lag_1day"]  = df["congestion"].shift(288)    # same time yesterday

    df = df.dropna()
    return df


def prepare_prophet_df(sensor_id: str, days: int = 14) -> pd.DataFrame:
    """
    Returns a DataFrame in Prophet's required format:
        ds  — datetime column
        y   — target (congestion 0.0–1.0)
    Plus extra regressors: weather_temp, weather_rain, is_holiday, is_weekend
    """
    city = "munich" if sensor_id.startswith("MUC") else "stuttgart"
    df   = load_sensor_data(sensor_id, days)
    df   = add_time_features(df, city)

    prophet_df = df.reset_index().rename(columns={"time": "ds", "congestion": "y"})
    prophet_df["ds"] = prophet_df["ds"].dt.tz_localize(None)  # Prophet needs tz-naive

    keep = ["ds", "y", "weather_temp", "weather_rain", "is_holiday", "is_weekend",
            "is_morning_rush", "is_evening_rush"]
    return prophet_df[keep]


def prepare_lstm_arrays(
    sensor_id: str,
    days: int = 14,
    window: int = 12,       # 12 × 5min = 60 min lookback
    horizon: int = 12,      # predict next 60 min (positions 6=30min, 12=60min)
):
    """
    Returns (X, y_30, y_60, scaler) for LSTM training.
    X shape: (samples, window, features)
    """
    from sklearn.preprocessing import StandardScaler

    city = "munich" if sensor_id.startswith("MUC") else "stuttgart"
    df   = load_sensor_data(sensor_id, days)
    df   = add_time_features(df, city)

    feature_cols = [
        "congestion", "speed_avg", "volume", "occupancy",
        "weather_temp", "weather_rain",
        "hour_sin", "hour_cos", "dow_sin", "dow_cos",
        "is_holiday", "is_weekend", "is_morning_rush", "is_evening_rush",
        "lag_30min", "lag_60min",
    ]
    feature_cols = [c for c in feature_cols if c in df.columns]

    scaler = StandardScaler()
    scaled = scaler.fit_transform(df[feature_cols].values)

    X, y_30, y_60 = [], [], []
    cong_idx = feature_cols.index("congestion")

    for i in range(window, len(scaled) - horizon):
        X.append(scaled[i - window:i])
        y_30.append(df["congestion"].iloc[i + 6])    # +30 min
        y_60.append(df["congestion"].iloc[i + 12])   # +60 min

    return np.array(X), np.array(y_30), np.array(y_60), scaler, feature_cols