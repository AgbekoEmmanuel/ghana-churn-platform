"""
Phase 4 - simulator.py  ("data-aware" live customer generator)
==============================================================
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

# Columns we write for each simulated customer. Note: no 'churn' (unknown
# future) and no 'gender' (we decided not to use it).
INSERT_COLUMNS = [c for c in lb.INSERT_COLUMNS if c not in ("churn", "gender")]


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
    return df


# ---------------------------------------------------------------------
# 3. Write them to Neon
# ---------------------------------------------------------------------
def insert_customers(conn, df: pd.DataFrame):
    rows = [
        tuple(v.item() if hasattr(v, "item") else v for v in rec) + ("simulator", True)
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
    parser.add_argument("--interval", type=float, default=300, help="seconds between batches (default 300)")
    parser.add_argument("--batch", type=int, default=3, help="max customers per batch (default 3)")
    parser.add_argument("--max-batches", type=int, default=0, help="stop after N batches (0 = run forever)")
    args = parser.parse_args()

    conn = psycopg2.connect(lb.DATABASE_URL)
    templates = load_templates(conn)
    print(f"Learned from {len(templates):,} real customers. Streaming every {args.interval}s. Ctrl+C to stop.\n")

    batches, total = 0, 0
    try:
        while True:
            try:
                n = int(rng.integers(1, args.batch + 1))          # 1..batch customers this round
                new = make_customers(templates, n, next_customer_id(conn))
                insert_customers(conn, new)
                batches += 1
                total += n
                ids = f"{new.customer_id.iloc[0]}-{new.customer_id.iloc[-1]}" if n > 1 else str(new.customer_id.iloc[0])
                print(f"[{datetime.now():%H:%M:%S}] +{n} customer(s)  ids {ids}  "
                      f"(total this run: {total})")
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
