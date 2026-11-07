"""
Phase 5 - scorer.py  (automated ML scoring loop)
================================================
Runs in the background next to the simulator. Every few seconds it:
  1. Finds new customers that have not been scored yet (scored_at IS NULL).
  2. Feeds them to the saved model (models/churn_model.pkl).
  3. Writes the results back to the same rows:
        churn_probability  e.g. 0.7312  (73% chance of leaving)
        risk_band          'low' / 'medium' / 'high'
        model_version      which model made the prediction (for auditing)
        scored_at          when it was scored
Then it sleeps and repeats.

Extra features:
  - HOT RELOAD: if you retrain the model (train_model.py), the scorer notices
    the new .pkl file and switches to it without being restarted.
  - Only simulated (live) customers are scored by default. The Kaggle rows
    were used to TRAIN the model, so scoring them would look unrealistically good.
    Use --include-history if you want them scored anyway.

Run (venv active), ideally in a SECOND terminal while the simulator runs:
    python scripts\\scorer.py
    python scripts\\scorer.py --interval 3
    python scripts\\scorer.py --once        # score whatever is waiting, then exit
Stop with Ctrl+C.
"""

import argparse
import os
import time
from datetime import datetime
from pathlib import Path

import joblib
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = PROJECT_DIR / "models" / "churn_model.pkl"
load_dotenv(PROJECT_DIR / ".env")
DATABASE_URL = os.getenv("DATABASE_URL")
BATCH_LIMIT = 500          # score at most this many rows per round


# ---------------------------------------------------------------------
# Model loading (with hot reload)
# ---------------------------------------------------------------------
class ModelHolder:
    """Keeps the model in memory and reloads it if the .pkl file changes."""

    def __init__(self, path: Path):
        self.path = path
        self.mtime = None
        self.bundle = None
        self.reload_if_changed()

    def reload_if_changed(self):
        if not self.path.exists():
            raise SystemExit("models/churn_model.pkl not found. Run train_model.py first (Phase 3).")
        mtime = self.path.stat().st_mtime          # last-modified time of the file
        if mtime != self.mtime:
            self.bundle = joblib.load(self.path)
            self.mtime = mtime
            b = self.bundle
            print(f"Loaded model {b['model_name']} {b['model_version']} "
                  f"(threshold {b['threshold']:.2f})")

    @property
    def threshold(self):
        return self.bundle["threshold"]


def risk_band(p: float, threshold: float) -> str:
    """
    Turn a probability into a simple label for business users:
      high   : at or above the model's decision threshold -> act now
      medium : at least half the threshold                -> keep an eye on
      low    : below that
    """
    if p >= threshold:
        return "high"
    if p >= threshold / 2:
        return "medium"
    return "low"


# ---------------------------------------------------------------------
# One scoring round
# ---------------------------------------------------------------------
def score_once(conn, model: ModelHolder, include_history: bool) -> int:
    features = model.bundle["features"]
    where = "scored_at IS NULL" + ("" if include_history else " AND data_source = 'simulator'")

    with conn, conn.cursor() as cur:               # one transaction per round
        # FOR UPDATE SKIP LOCKED: if two scorers ever run at once, they
        # won't grab the same rows. (A standard 'job queue' trick.)
        cur.execute(f"""
            SELECT id, {", ".join(features)}
            FROM bank_customers
            WHERE {where}
            ORDER BY created_at
            LIMIT {BATCH_LIMIT}
            FOR UPDATE SKIP LOCKED
        """)
        rows = cur.fetchall()
        if not rows:
            return 0

        df = pd.DataFrame(rows, columns=["id"] + features)
        for c in features:                         # Decimal -> float for numeric columns
            if c not in ("country", "region", "primary_channel"):
                df[c] = pd.to_numeric(df[c])

        # The saved pipeline does all preprocessing (one-hot etc.) itself.
        proba = model.bundle["pipeline"].predict_proba(df[features])[:, 1]

        version = model.bundle["model_version"]
        updates = [(int(i), round(float(p), 4), risk_band(p, model.threshold), version)
                   for i, p in zip(df["id"], proba)]

        # Update many rows in one statement by joining to a VALUES list.
        execute_values(cur, """
            UPDATE bank_customers AS b
            SET churn_probability = v.p::numeric,
                risk_band        = v.band,
                model_version    = v.ver,
                scored_at        = NOW()
            FROM (VALUES %s) AS v(id, p, band, ver)
            WHERE b.id = v.id
        """, updates)

    bands = pd.Series([u[2] for u in updates]).value_counts()
    print(f"[{datetime.now():%H:%M:%S}] scored {len(updates):>3}  "
          f"high {bands.get('high', 0):>3} | medium {bands.get('medium', 0):>3} | low {bands.get('low', 0):>3}")
    return len(updates)


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Score unscored customers in Neon")
    parser.add_argument("--interval", type=float, default=5, help="seconds between rounds (default 5)")
    parser.add_argument("--once", action="store_true", help="score what is waiting, then exit")
    parser.add_argument("--include-history", action="store_true", help="also score the Kaggle training rows")
    args = parser.parse_args()

    model = ModelHolder(MODEL_PATH)
    conn = psycopg2.connect(DATABASE_URL)
    print(f"Scorer running. Checking every {args.interval}s. Ctrl+C to stop.\n")

    try:
        while True:
            try:
                model.reload_if_changed()
                n = score_once(conn, model, args.include_history)
                # If a full batch came back there may be more waiting: go again at once.
                if n == BATCH_LIMIT:
                    continue
            except psycopg2.OperationalError as e:
                print(f"Connection problem ({str(e).strip()[:60]}...). Reconnecting in 5s.")
                time.sleep(5)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = psycopg2.connect(DATABASE_URL)
                continue

            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped by you (Ctrl+C).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
