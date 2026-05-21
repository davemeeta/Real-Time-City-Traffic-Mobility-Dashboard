"""
train_lstm.py
-------------
Trains a stacked 2-layer LSTM per sensor for 30 and 60 min congestion
forecasting. Tracks experiments with MLflow (free, local).

Run:
    python train_lstm.py

Then view MLflow UI:
    mlflow ui --port 5001
    open http://localhost:5001
"""

import os
import pickle
import logging
import warnings
import numpy as np
import mlflow
import mlflow.keras

import tensorflow as tf
import keras
from keras import layers
from sklearn.model_selection import train_test_split

from features import SENSOR_IDS, prepare_lstm_arrays

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODELS_DIR  = os.path.join(os.path.dirname(__file__), "models")
os.makedirs(MODELS_DIR, exist_ok=True)

# MLflow — stores locally in ./mlruns (free, no server needed)
mlflow.set_tracking_uri("./mlruns")
mlflow.set_experiment("traffic-congestion-lstm")

# Hyperparameters
WINDOW_SIZE  = 12     # 60 min lookback
LSTM_UNITS_1 = 64
LSTM_UNITS_2 = 32
DROPOUT      = 0.2
EPOCHS       = 30
BATCH_SIZE   = 32
PATIENCE     = 5      # early stopping


def build_model(input_shape: tuple) -> keras.Model:
    """
    Stacked 2-layer LSTM with two output heads:
      - output_30: congestion in 30 min
      - output_60: congestion in 60 min
    """
    inputs = keras.Input(shape=input_shape, name="sensor_sequence")

    x = layers.LSTM(LSTM_UNITS_1, return_sequences=True,
                    name="lstm_1")(inputs)
    x = layers.Dropout(DROPOUT, name="dropout_1")(x)

    x = layers.LSTM(LSTM_UNITS_2, return_sequences=False,
                    name="lstm_2")(x)
    x = layers.Dropout(DROPOUT, name="dropout_2")(x)

    x = layers.Dense(16, activation="relu", name="dense_shared")(x)

    # Two separate output heads
    out_30 = layers.Dense(1, activation="sigmoid", name="output_30")(x)
    out_60 = layers.Dense(1, activation="sigmoid", name="output_60")(x)

    model = keras.Model(inputs=inputs, outputs=[out_30, out_60])
    model.compile(
        optimizer=keras.optimizers.Adam(learning_rate=0.001),
        loss={"output_30": "mse", "output_60": "mse"},
        metrics={"output_30": "mae", "output_60": "mae"},
    )
    return model


def train_sensor(sensor_id: str):
    log.info(f"\n[{sensor_id}] Preparing LSTM data…")

    try:
        X, y_30, y_60, scaler, feature_cols = prepare_lstm_arrays(
            sensor_id, days=14, window=WINDOW_SIZE
        )
    except Exception as e:
        log.error(f"[{sensor_id}] Data error: {e}")
        return

    if len(X) < 100:
        log.warning(f"[{sensor_id}] Too few samples ({len(X)}) — skipping.")
        return

    # Train / val split (80/20), no shuffle (time-series!)
    split = int(len(X) * 0.8)
    X_tr,  X_val  = X[:split],   X[split:]
    y30_tr, y30_val = y_30[:split], y_30[split:]
    y60_tr, y60_val = y_60[:split], y_60[split:]

    log.info(f"[{sensor_id}] Train: {len(X_tr)} samples  Val: {len(X_val)} samples")
    log.info(f"[{sensor_id}] Input shape: {X_tr.shape}")

    model = build_model(input_shape=(WINDOW_SIZE, X_tr.shape[2]))

    callbacks = [
        keras.callbacks.EarlyStopping(
            monitor="val_loss", patience=PATIENCE,
            restore_best_weights=True, verbose=1,
        ),
        keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss", factor=0.5,
            patience=3, min_lr=1e-5, verbose=1,
        ),
    ]

    with mlflow.start_run(run_name=sensor_id):
        # Log hyperparameters
        mlflow.log_params({
            "sensor_id":   sensor_id,
            "window_size": WINDOW_SIZE,
            "lstm_units_1":LSTM_UNITS_1,
            "lstm_units_2":LSTM_UNITS_2,
            "dropout":     DROPOUT,
            "epochs":      EPOCHS,
            "batch_size":  BATCH_SIZE,
            "features":    len(feature_cols),
        })

        history = model.fit(
            X_tr,
            {"output_30": y30_tr, "output_60": y60_tr},
            validation_data=(
                X_val,
                {"output_30": y30_val, "output_60": y60_val},
            ),
            epochs=EPOCHS,
            batch_size=BATCH_SIZE,
            callbacks=callbacks,
            verbose=1,
        )

        # Evaluate
        y30_pred, y60_pred = model.predict(X_val, verbose=0)
        mae_30 = float(np.mean(np.abs(y30_val - y30_pred.flatten())))
        mae_60 = float(np.mean(np.abs(y60_val - y60_pred.flatten())))
        rmse_30 = float(np.sqrt(np.mean((y30_val - y30_pred.flatten())**2)))
        rmse_60 = float(np.sqrt(np.mean((y60_val - y60_pred.flatten())**2)))

        mlflow.log_metrics({
            "mae_30min":  mae_30,
            "mae_60min":  mae_60,
            "rmse_30min": rmse_30,
            "rmse_60min": rmse_60,
        })

        log.info(f"[{sensor_id}] 30min → MAE: {mae_30:.4f}  RMSE: {rmse_30:.4f}")
        log.info(f"[{sensor_id}] 60min → MAE: {mae_60:.4f}  RMSE: {rmse_60:.4f}")

        # Save model + scaler
        model_path  = os.path.join(MODELS_DIR, f"lstm_{sensor_id}.keras")
        scaler_path = os.path.join(MODELS_DIR, f"scaler_{sensor_id}.pkl")

        model.save(model_path)
        with open(scaler_path, "wb") as f:
            pickle.dump({"scaler": scaler, "feature_cols": feature_cols}, f)

        mlflow.log_artifact(model_path)
        log.info(f"[{sensor_id}] ✓ Model saved → {model_path}")

    return {"mae_30": mae_30, "mae_60": mae_60}


def main():
    log.info("Starting LSTM training for all sensors…")
    log.info(f"TensorFlow version: {tf.__version__}")

    results = {}
    for sensor_id in SENSOR_IDS:
        r = train_sensor(sensor_id)
        if r:
            results[sensor_id] = r

    # Final summary
    if results:
        log.info("\n" + "="*55)
        log.info("LSTM TRAINING COMPLETE — copy these metrics to your README")
        log.info("="*55)
        avg_mae_30 = np.mean([v["mae_30"] for v in results.values()])
        avg_mae_60 = np.mean([v["mae_60"] for v in results.values()])
        log.info(f"Avg MAE  30-min forecast: {avg_mae_30:.4f}")
        log.info(f"Avg MAE  60-min forecast: {avg_mae_60:.4f}")
        log.info(f"Models saved in: {MODELS_DIR}")
        log.info("\nView experiment runs:")
        log.info("  mlflow ui --port 5001")
        log.info("  open http://localhost:5001")
        log.info("="*55)


if __name__ == "__main__":
    main()