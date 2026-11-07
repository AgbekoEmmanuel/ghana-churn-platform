"""
Phase 4/8 - simulator.py  ("data-aware" live customer generator + ageing)
==========================================================================
Pretends to be the bank's core system: every few seconds it creates a few
NEW, realistic customers and writes them into Neon, where the scoring
script (Phase 5) and the web platform (Phase 6) will pick them up.

How it stays realistic ("data-aware"):
  1. At start-up it reads the 10,000 REAL Kaggle customers from Neon.
  2. For each new customer it picks a random real customer as a TEMPLATE
     and changes the details a little (age +/- a few years, balance +/- ~10%,
     etc.). This keeps realistic combinations: e.g. a 25-year-old won't get
     a 30-year tenure, because real 25-year-olds don't have one.
  3. It then generates the Ghana channel and 360 activity columns with the
     SAME rules as Phase 2 (imported from load_baseline.py), so live data
     looks like the training data.

Ageing (Phase 8 - dormancy):
  Every round also moves the clock forward by one SIMULATED DAY (--days-per-run)
  for every live customer. Each customer either does something that day
  (days_since_last_txn resets to 0) or stays quiet (+1 day). Customers who are
  secretly "drifting away" get quieter and quieter until they pass 90 idle days
  and become DORMANT. Changed customers are queued for re-scoring.
  With a run every 5 minutes, one simulated day passes every 5 minutes, so
  dormancy shows up on the platform within hours instead of months.

The hidden future:
  Each new customer secretly "will" or "won't" churn (copied from the
  template, with some randomness). That hidden outcome shapes their
  behaviour (fewer transactions, more failed ones...) but is NOT saved:
  'churn' stays NULL, exactly like in real life, where you don't know the
  future yet. The model must work it out from behaviour alone.

Run (venv active):
    python scripts\\simulator.py                  # 1-3 customers every 5 seconds, forever
    python scripts\\simulator.py --interval 2 --batch 5
    python scripts\\simulator.py --max-batches 3  # quick test: 3 rounds then stop
    python scripts\\simulator.py --days-per-run 0  # turn ageing off
Stop it any time with Ctrl+C.
"""

import argparse
import time
from datetime import datetime

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

# Re-use the exact same generators and settings from Phase 2.
# (Python can import it because both scripts live in the scripts/ folder.)
import load_baseline as lb

# Phase 2 uses a FIXED seed so its results are reproducible.
# The simulator wants fresh randomness every run, so we replace it.
lb.rng = np.random.default_rng()
rng = lb.rng

BASE_COLUMNS = ["credit_score", "country", "age", "tenure", "balance",
                "products_number", "credit_card", "active_member",
                "estimated_salary", "churn"]

# Columns we write for each simulated customer. Not written: 'churn' and
# 'went_dormant_90d' (unknown future) and 'gender' (we decided not to use it).
# Added: the two hidden sim_ columns that drive ageing.
INSERT_COLUMNS = [c for c in lb.INSERT_COLUMNS if c not in ("churn", "gender", "went_dormant_90d")] \
                 + ["sim_activity_prob", "sim_goes_dormant"]


# ---------------------------------------------------------------------
# 1. Learn from the real data
# ---------------------------------------------------------------------
def load_templates(conn) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT {", ".join(BASE_COLUMNS)}
            FROM bank_customers
            WHERE data_source = 'kaggle' AND churn IS NOT NULL
        """)
        df = pd.DataFrame(cur.fetchall(), columns=BASE_COLUMNS)
    for c in BASE_COLUMNS:
        if c != "country":
            df[c] = pd.to_numeric(df[c])
    if df.empty:
        raise SystemExit("No Kaggle rows found. Run load_baseline.py first (Phase 2).")
    return df


def next_customer_id(conn) -> int:
    """New customers get IDs after the highest one already in the table."""
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(customer_id), 0) FROM bank_customers")
        return int(cur.fetchone()[0]) + 1


# ---------------------------------------------------------------------
# 2. Manufacture new customers
# ---------------------------------------------------------------------
def make_customers(templates: pd.DataFrame, n: int, first_id: int) -> pd.DataFrame:
    # Pick n real customers at random to use as templates.
    df = templates.sample(n=n, replace=True, random_state=rng.integers(1_000_000_000)).reset_index(drop=True)

    # Nudge each number a little so new customers are similar, not identical.
    df["age"] = np.clip(df["age"] + rng.integers(-3, 4, n), 18, 92)
    df["tenure"] = np.clip(df["tenure"] + rng.integers(-1, 2, n), 0, 10)
    df["credit_score"] = np.clip(df["credit_score"] + rng.integers(-25, 26, n), 350, 850)
    df["balance"] = np.where(df["balance"] > 0,
                             (df["balance"] * rng.normal(1, 0.10, n)).clip(0), 0).round(2)
    df["estimated_salary"] = (df["estimated_salary"] * rng.normal(1, 0.10, n)).clip(100).round(2)

    # The hidden future: mostly follows the template, sometimes flips (10%).
    flip = rng.random(n) < 0.10
    df["churn"] = np.where(flip, 1 - df["churn"], df["churn"]).astype(int)

    df.insert(0, "customer_id", np.arange(first_id, first_id + n))

    # Same Ghana + 360 rules as Phase 2. They use the hidden 'churn' to shape behaviour.
    df = lb.add_ghana_features(df)
    df = lb.add_activity_features(df)

    # Hidden dormancy behaviour (never shown to the model).
    already_dormant = df["days_since_last_txn"].to_numpy() >= lb.DORMANT_AFTER_DAYS
    drifting = rng.random(n) < lb.dormancy_propensity(df)
    df["sim_goes_dormant"] = already_dormant | drifting
    df["sim_activity_prob"] = np.where(already_dormant, 0.01, lb.daily_activity_prob(df)).round(4)
    return df


# ---------------------------------------------------------------------
# 2b. Age existing customers by N simulated days (Phase 8)
# ---------------------------------------------------------------------
AGE_SQL = """
WITH today AS (                       -- roll the dice once per customer
    SELECT id, random() < sim_activity_prob AS did_something
    FROM bank_customers
    WHERE data_source = 'simulator' AND sim_activity_prob IS NOT NULL
)
UPDATE bank_customers AS b SET
    -- any activity resets the idle counter; otherwise it grows
    days_since_last_txn = CASE WHEN t.did_something THEN 0
                               ELSE b.days_since_last_txn + %(days)s END,
    -- 90-day activity counts: +1 when active, wiped once idle for 90+ days
    total_txn_count_90d = CASE WHEN t.did_something THEN LEAST(b.total_txn_count_90d + 1, 300)
                               WHEN b.days_since_last_txn + %(days)s >= 90 THEN 0
                               ELSE b.total_txn_count_90d END,
    branch_visits_90d   = CASE WHEN NOT t.did_something AND b.days_since_last_txn + %(days)s >= 90 THEN 0
                               ELSE b.branch_visits_90d END,
    atm_withdrawals_90d = CASE WHEN NOT t.did_something AND b.days_since_last_txn + %(days)s >= 90 THEN 0
                               ELSE b.atm_withdrawals_90d END,
    -- customers drifting away get quieter every day
    sim_activity_prob   = CASE WHEN b.sim_goes_dormant
                               THEN GREATEST(b.sim_activity_prob * power(0.8, %(days)s), 0.002)
                               ELSE b.sim_activity_prob END,
    scored_at = NULL                  -- features changed -> score again
FROM today AS t
WHERE b.id = t.id
"""


def age_customers(conn, days: int) -> int:
    """Move every live customer forward by `days` simulated days. Returns rows changed."""
    if days <= 0:
        return 0
    with conn, conn.cursor() as cur:
        cur.execute(AGE_SQL, {"days": days})
        return cur.rowcount


# ---------------------------------------------------------------------
# 3. Write them to Neon
# ---------------------------------------------------------------------
def insert_customers(conn, df: pd.DataFrame):
    rows = [
        tuple(None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in rec)
        + ("simulator", True)
        for rec in df[INSERT_COLUMNS].itertuples(index=False)
    ]
    sql = f"""
        INSERT INTO bank_customers ({", ".join(INSERT_COLUMNS)}, data_source, ghana_features_synthetic)
        VALUES %s
    """
    with conn, conn.cursor() as cur:            # commit on success
        execute_values(cur, sql, rows)


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Stream simulated bank customers into Neon")
    parser.add_argument("--interval", type=float, default=5, help="seconds between batches (default 5)")
    parser.add_argument("--batch", type=int, default=3, help="max customers per batch (default 3)")
    parser.add_argument("--max-batches", type=int, default=0, help="stop after N batches (0 = run forever)")
    parser.add_argument("--days-per-run", type=int, default=1, help="simulated days that pass each round (0 = no ageing)")
    args = parser.parse_args()

    conn = psycopg2.connect(lb.DATABASE_URL)
    templates = load_templates(conn)
    print(f"Learned from {len(templates):,} real customers. Streaming every {args.interval}s. Ctrl+C to stop.\n")

    batches, total = 0, 0
    try:
        while True:
            try:
                aged = age_customers(conn, args.days_per_run)    # existing customers get older first
                n = int(rng.integers(1, args.batch + 1))          # 1..batch customers this round
                new = make_customers(templates, n, next_customer_id(conn))
                insert_customers(conn, new)
                batches += 1
                total += n
                ids = f"{new.customer_id.iloc[0]}-{new.customer_id.iloc[-1]}" if n > 1 else str(new.customer_id.iloc[0])
                print(f"[{datetime.now():%H:%M:%S}] +{n} customer(s)  ids {ids}  "
                      f"(total this run: {total})"
                      + (f"  | aged {aged} customers by {args.days_per_run} day(s)" if aged else ""))
            except psycopg2.OperationalError as e:
                # Wi-Fi blip or Neon waking up: reconnect and carry on.
                print(f"Connection problem ({str(e).strip()[:60]}...). Reconnecting in 5s.")
                time.sleep(5)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = psycopg2.connect(lb.DATABASE_URL)
                continue

            if args.max_batches and batches >= args.max_batches:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped by you (Ctrl+C).")
    finally:
        conn.close()
        print(f"Simulator finished. {total} customers added this run.")


if __name__ == "__main__":
    main()
