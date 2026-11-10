"""
Phase 5/8 - scorer.py  (automated ML scoring loop: churn + dormancy)
====================================================================
Runs in the background next to the simulator. Every few seconds it:
  1. Finds new customers that have not been scored yet (scored_at IS NULL).
  2. Feeds them to the saved model (models/churn_model.pkl).
  3. Writes the results back to the same rows:
        churn_probability  e.g. 0.7312  (73% chance of leaving)
        risk_band          'low' / 'medium' / 'high'
        model_version      which model made the prediction (for auditing)
        scored_at          when it was scored
     and, if models/dormancy_model.pkl exists (Phase 8):
        dormancy_status       'dormant' if no activity for 90+ days, else 'active'  (a RULE)
        dormancy_probability  chance an ACTIVE customer goes dormant in 90 days (the MODEL)
        dormancy_band         'low' / 'medium' / 'high'
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
DORMANCY_PATH = PROJECT_DIR / "models" / "dormancy_model.pkl"
DORMANT_AFTER_DAYS = 90       # business rule: 3 months with no activity of any kind
load_dotenv(PROJECT_DIR / ".env")
DATABASE_URL = os.getenv("DATABASE_URL")
BATCH_LIMIT = 500          # score at most this many rows per round


# ---------------------------------------------------------------------
# Model loading (with hot reload)
# ---------------------------------------------------------------------
class ModelHolder:
    """Keeps the model in memory and reloads it if the .pkl file changes."""

    def __init__(self, path: Path, required: bool = True):
        self.path = path
        self.required = required
        self.mtime = None
        self.bundle = None
        self.reload_if_changed()

    def reload_if_changed(self):
        if not self.path.exists():
            if self.required:
                raise SystemExit(f"models/{self.path.name} not found. Train it first.")
            return                                   # optional model not trained yet
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
def score_once(conn, model: ModelHolder, dormancy: ModelHolder, include_history: bool) -> int:
    features = list(model.bundle["features"])
    if dormancy.bundle:                              # add any extra columns the dormancy model needs
        features += [f for f in dormancy.bundle["features"] if f not in features]
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
        proba = model.bundle["pipeline"].predict_proba(df[model.bundle["features"]])[:, 1]

        # Dormancy: a RULE for who is dormant now, the MODEL for who is drifting.
        is_dormant = (df["days_since_last_txn"] >= DORMANT_AFTER_DAYS).to_numpy()
        if dormancy.bundle:
            d_proba = dormancy.bundle["pipeline"].predict_proba(df[dormancy.bundle["features"]])[:, 1]
        else:
            d_proba = [None] * len(df)

        version = model.bundle["model_version"]
        updates = []
        for i, p, dormant, dp in zip(df["id"], proba, is_dormant, d_proba):
            if dormant:
                d_status, d_p, d_band = "dormant", None, None        # already dormant: nothing to predict
            elif dp is None:
                d_status, d_p, d_band = "active", None, None        # dormancy model not trained yet
            else:
                d_status, d_p, d_band = "active", round(float(dp), 4), risk_band(dp, dormancy.threshold)
            updates.append((int(i), round(float(p), 4), risk_band(p, model.threshold), version,
                            d_status, d_p, d_band))

        # Update many rows in one statement by joining to a VALUES list.
        execute_values(cur, """
            UPDATE bank_customers AS b
            SET churn_probability    = v.p::numeric,
                risk_band            = v.band,
                model_version        = v.ver,
                dormancy_status      = v.d_status,
                dormancy_probability = v.d_p::numeric,
                dormancy_band        = v.d_band,
                scored_at            = NOW()
            FROM (VALUES %s) AS v(id, p, band, ver, d_status, d_p, d_band)
            WHERE b.id = v.id
        """, updates)

    bands = pd.Series([u[2] for u in updates]).value_counts()
    dormant_now = sum(u[4] == "dormant" for u in updates)
    drifting = sum(u[6] == "high" for u in updates)
    print(f"[{datetime.now():%H:%M:%S}] scored {len(updates):>4}  churn high {bands.get('high', 0):>3} | "
          f"medium {bands.get('medium', 0):>3} | low {bands.get('low', 0):>3}  || "
          f"dormant {dormant_now:>3} | going dormant (high) {drifting:>3}")
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
    dormancy = ModelHolder(DORMANCY_PATH, required=False)
    if not dormancy.bundle:
        print("Note: models/dormancy_model.pkl not found, so only churn is scored.")
    conn = psycopg2.connect(DATABASE_URL)
    print(f"Scorer running. Checking every {args.interval}s. Ctrl+C to stop.\n")

    try:
        while True:
            try:
                model.reload_if_changed()
                dormancy.reload_if_changed()
                n = score_once(conn, model, dormancy, args.include_history)
                # If a full batch came back there may be more waiting: go again at once.
                if n == BATCH_LIMIT:
                    continue
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                # Neon can drop idle/long connections. When that happens mid-transaction,
                # `with conn` raises InterfaceError while rolling back, so catch both.
                print(f"Connection problem ({str(e).strip()[:60]}...). Reconnecting in 5s.")
                time.sleep(5)
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    conn = psycopg2.connect(DATABASE_URL)
                except psycopg2.OperationalError as e2:
                    print(f"Reconnect failed ({str(e2).strip()[:60]}...). Will retry.")
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
