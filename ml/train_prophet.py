"""
train_prophet.py
----------------
Trains a Prophet model per sensor and writes 30/60 min forecasts
to the `forecasts` table in TimescaleDB.

Run:
    python train_prophet.py

Re-run anytime to refresh forecasts. Set up a cron job for automatic retraining:
    */30 * * * * cd /path/to/traffic-dashboard/ml && python train_prophet.py
"""

import os
import logging
import pickle
import warnings
from datetime import datetime, timezone, timedelta

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from prophet import Prophet

from features import SENSOR_IDS, prepare_prophet_df

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     "localhost",
    "port":     5433,
    "dbname":   "traffic_db",
    "user":     "traffic_user",
    "password": "traffic_pass",
}

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
os.makedirs(MODELS_DIR, exist_ok=True)


def train_sensor(sensor_id: str) -> Prophet | None:
    """Train Prophet on one sensor's data. Returns fitted model."""
    log.info(f"[{sensor_id}] Loading data…")
    try:
        df = prepare_prophet_df(sensor_id, days=14)
    except Exception as e:
        log.error(f"[{sensor_id}] Data load failed: {e}")
        return None

    if len(df) < 200:
        log.warning(f"[{sensor_id}] Too few rows ({len(df)}) — skipping.")
        return None

    log.info(f"[{sensor_id}] Training Prophet on {len(df):,} rows…")

    model = Prophet(
        changepoint_prior_scale=0.05,    # conservative — traffic is predictable
        seasonality_prior_scale=10,
        holidays_prior_scale=10,
        daily_seasonality=True,
        weekly_seasonality=True,
        yearly_seasonality=False,        # only 14 days of data
        interval_width=0.80,             # 80% confidence band
    )

    # Add extra regressors
    for reg in ["weather_temp", "weather_rain", "is_holiday",
                "is_weekend", "is_morning_rush", "is_evening_rush"]:
        model.add_regressor(reg)

    model.fit(df)

    # Save model to disk
    model_path = os.path.join(MODELS_DIR, f"prophet_{sensor_id}.pkl")
    with open(model_path, "wb") as f:
        pickle.dump({"model": model, "trained_at": datetime.now(timezone.utc)}, f)
    log.info(f"[{sensor_id}] Model saved → {model_path}")

    return model, df


def forecast_sensor(sensor_id: str, model: Prophet, train_df: pd.DataFrame) -> list[dict]:
    """
    Generate 30 and 60 min forecasts.
    Returns list of dicts ready for DB insert.
    """
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    t30 = now + timedelta(minutes=30)
    t60 = now + timedelta(minutes=60)

    # Build future DataFrame for the two forecast horizons
    future_rows = []
    for target_time in [t30, t60]:
        t_local = target_time.astimezone()
        hour    = t_local.hour
        dow     = t_local.weekday()
        is_we   = 1 if dow >= 5 else 0
        future_rows.append({
            "ds":               target_time.replace(tzinfo=None),
            "weather_temp":     train_df["weather_temp"].iloc[-1],   # use latest known
            "weather_rain":     train_df["weather_rain"].iloc[-1],
            "is_holiday":       0,
            "is_weekend":       is_we,
            "is_morning_rush":  1 if hour in (7, 8, 9) else 0,
            "is_evening_rush":  1 if hour in (16, 17, 18) else 0,
        })

    future_df  = pd.DataFrame(future_rows)
    forecast   = model.predict(future_df)

    results = []
    for i, (target_time, horizon_min) in enumerate([(t30, 30), (t60, 60)]):
        pred  = float(forecast["yhat"].iloc[i])
        lo    = float(forecast["yhat_lower"].iloc[i])
        hi    = float(forecast["yhat_upper"].iloc[i])

        # Clamp to [0, 1]
        pred  = max(0.0, min(1.0, pred))
        lo    = max(0.0, min(1.0, lo))
        hi    = max(0.0, min(1.0, hi))

        results.append({
            "sensor_id":        sensor_id,
            "generated_at":     now.isoformat(),
            "horizon_min":      horizon_min,
            "forecast_time":    target_time.isoformat(),
            "congestion_pred":  round(pred, 4),
            "congestion_lo":    round(lo, 4),
            "congestion_hi":    round(hi, 4),
            "model_name":       "prophet",
        })

    return results


def write_forecasts(all_forecasts: list[dict]):
    """Upsert all forecasts to TimescaleDB."""
    if not all_forecasts:
        return

    conn   = psycopg2.connect(**DB_CONFIG)
    cursor = conn.cursor()

    rows = [
        (
            f["sensor_id"], f["generated_at"], f["horizon_min"],
            f["forecast_time"], f["congestion_pred"],
            f["congestion_lo"], f["congestion_hi"], f["model_name"],
        )
        for f in all_forecasts
    ]

    execute_values(cursor, """
        INSERT INTO forecasts
            (sensor_id, generated_at, horizon_min, forecast_time,
             congestion_pred, congestion_lo, congestion_hi, model_name)
        VALUES %s
        ON CONFLICT (sensor_id, horizon_min, forecast_time)
        DO UPDATE SET
            congestion_pred = EXCLUDED.congestion_pred,
            congestion_lo   = EXCLUDED.congestion_lo,
            congestion_hi   = EXCLUDED.congestion_hi,
            generated_at    = EXCLUDED.generated_at
    """, rows)

    conn.commit()
    cursor.close()
    conn.close()
    log.info(f"Wrote {len(rows)} forecast rows to DB.")


def evaluate(sensor_id: str, model: Prophet, train_df: pd.DataFrame):
    """
    Quick in-sample accuracy check — prints MAE and RMSE.
    Good numbers to put in your README / recruiter portfolio.
    """
    import numpy as np

    # Use last 20% of training data as pseudo-validation
    split      = int(len(train_df) * 0.8)
    val_df     = train_df.iloc[split:].copy()
    forecast   = model.predict(val_df)

    y_true = val_df["y"].values
    y_pred = forecast["yhat"].values

    mae  = float(np.mean(np.abs(y_true - y_pred)))
    rmse = float(np.sqrt(np.mean((y_true - y_pred) ** 2)))
    log.info(f"[{sensor_id}] Validation — MAE: {mae:.4f}  RMSE: {rmse:.4f}")
    return {"mae": mae, "rmse": rmse}


def main():
    all_forecasts = []
    metrics       = {}

    for sensor_id in SENSOR_IDS:
        result = train_sensor(sensor_id)
        if result is None:
            continue

        model, train_df = result
        forecasts = forecast_sensor(sensor_id, model, train_df)
        all_forecasts.extend(forecasts)

        m = evaluate(sensor_id, model, train_df)
        metrics[sensor_id] = m

    write_forecasts(all_forecasts)

    # Summary for your README
    log.info("\n" + "="*50)
    log.info("FORECAST ACCURACY SUMMARY (put this in your README!)")
    log.info("="*50)
    if metrics:
        import numpy as np
        avg_mae  = float(np.mean([v["mae"]  for v in metrics.values()]))
        avg_rmse = float(np.mean([v["rmse"] for v in metrics.values()]))
        log.info(f"Average MAE  across {len(metrics)} sensors: {avg_mae:.4f}")
        log.info(f"Average RMSE across {len(metrics)} sensors: {avg_rmse:.4f}")
    log.info(f"Forecasts written for {len(all_forecasts)} sensor×horizon pairs.")
    log.info("="*50)


if __name__ == "__main__":
    main()