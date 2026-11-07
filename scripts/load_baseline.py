"""
Phase 2 - load_baseline.py  (v2: now with the 360-degree activity view)
==========================
Loads the real Kaggle bank churn CSV into Neon as our historical baseline,
and adds SYNTHETIC Ghana digital-channel columns on the way in.

What it does, step by step:
  1. Reads dataset/bank_churn.csv with pandas.
  2. Checks the columns and cleans obvious problems (duplicates, blanks).
  3. Generates the Ghana columns (region, USSD, app, internet banking,
     MoMo link, primary channel, failed transactions) using simple,
     documented rules so they relate plausibly to the real columns.
  3b. Generates the 360 ACTIVITY columns (branch walk-ins, ATM, salary
     credits, complaints, recency, balance trend, total activity).
  4. Saves a copy as dataset/bank_churn_ghana.csv so you can open it in Excel.
  5. Bulk-inserts everything into the bank_customers table in Neon.
  6. Prints a short summary so you can see what was loaded.

Run from the project folder (with venv active):
    python scripts\\load_baseline.py

It is safe to run more than once: existing customers are UPDATED with the
latest generated columns (an 'upsert'), never duplicated.
"""

import os
from pathlib import Path

import numpy as np                       # fast maths and random numbers
import pandas as pd                      # tables (DataFrames)
import psycopg2                          # PostgreSQL driver
from psycopg2.extras import execute_values  # sends many rows in one go (fast)
from dotenv import load_dotenv

# ---------------------------------------------------------------------
# 0. Paths and settings
# ---------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent   # bank_churn_pipeline/
CSV_IN = PROJECT_DIR / "dataset" / "bank_churn.csv"
CSV_OUT = PROJECT_DIR / "dataset" / "bank_churn_ghana.csv"

# A fixed "seed" makes the random numbers the same every run.
# That way your results are reproducible (very important in data science).
rng = np.random.default_rng(seed=42)

load_dotenv(PROJECT_DIR / ".env")
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL not found in .env")

# The 12 columns we expect from Kaggle (Gaurav Topre's version).
EXPECTED = [
    "customer_id", "credit_score", "country", "gender", "age", "tenure",
    "balance", "products_number", "credit_card", "active_member",
    "estimated_salary", "churn",
]

# Some versions of this Kaggle dataset use different names
# (RowNumber, CustomerId, Exited...). This map renames them to ours.
RENAME = {
    "CustomerId": "customer_id", "CreditScore": "credit_score",
    "Geography": "country", "Gender": "gender", "Age": "age",
    "Tenure": "tenure", "Balance": "balance", "NumOfProducts": "products_number",
    "HasCrCard": "credit_card", "IsActiveMember": "active_member",
    "EstimatedSalary": "estimated_salary", "Exited": "churn",
}


# ---------------------------------------------------------------------
# 1. Read and clean the real data
# ---------------------------------------------------------------------
def load_kaggle_csv() -> pd.DataFrame:
    print(f"Reading {CSV_IN.name} ...")
    df = pd.read_csv(CSV_IN)
    df = df.rename(columns=RENAME)

    missing = [c for c in EXPECTED if c not in df.columns]
    if missing:
        raise SystemExit(f"These columns are missing from the CSV: {missing}")

    df = df[EXPECTED]                                  # keep only what we need, in order
    before = len(df)
    df = df.drop_duplicates(subset="customer_id")     # one row per customer
    df = df.dropna()                                  # drop rows with blanks
    print(f"  {before:,} rows read, {len(df):,} kept after cleaning")
    return df


# ---------------------------------------------------------------------
# 2. Generate the SYNTHETIC Ghana columns
# ---------------------------------------------------------------------
def sigmoid(x):
    """Squashes any number into a probability between 0 and 1."""
    return 1 / (1 + np.exp(-x))


def add_ghana_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Each rule below is an ASSUMPTION, not a measured fact. We write them
    down so anyone reviewing the project can see exactly how the
    synthetic columns were made.
    """
    n = len(df)
    age = df["age"].to_numpy()
    active = df["active_member"].to_numpy()
    churn = df["churn"].to_numpy()
    salary_k = df["estimated_salary"].to_numpy() / 1000   # salary in thousands
    has_balance = (df["balance"].to_numpy() > 0).astype(int)

    # --- Region: random, weighted roughly toward the big urban regions.
    # (Not linked to Kaggle's 'country', so we don't invent regional churn patterns.)
    regions = ["Greater Accra", "Ashanti", "Western", "Eastern", "Central",
               "Northern", "Volta", "Bono", "Upper East", "Upper West"]
    weights = [0.30, 0.20, 0.09, 0.09, 0.08, 0.07, 0.06, 0.05, 0.03, 0.03]
    df["region"] = rng.choice(regions, size=n, p=weights)

    # --- Mobile app: younger and active customers use it more.
    # Churners are a bit LESS likely to use it (disengaged customers).
    p_app = sigmoid(1.2 - 0.06 * (age - 35) + 0.8 * active - 0.6 * churn)
    df["uses_mobile_app"] = (rng.random(n) < p_app).astype(int)

    # --- USSD: very common in Ghana, works on any phone, no data needed.
    # Older customers and those without the app lean on it more.
    p_ussd = sigmoid(0.9 + 0.03 * (age - 35) + 0.7 * (1 - df["uses_mobile_app"]))
    df["uses_ussd"] = (rng.random(n) < p_ussd).astype(int)

    # --- Internet banking: less common; higher earners use it more.
    p_web = sigmoid(-1.6 + 0.008 * (salary_k - 100) + 0.4 * has_balance)
    df["uses_internet_banking"] = (rng.random(n) < p_web).astype(int)

    # --- MoMo link: account connected to a Mobile Money wallet.
    # Linked accounts are "stickier", so churners are a bit less often linked.
    p_momo = sigmoid(0.6 + 0.5 * df["uses_mobile_app"] + 0.3 * df["uses_ussd"] - 0.7 * churn)
    df["momo_linked"] = (rng.random(n) < p_momo).astype(int)

    # --- Failed transactions in the last 90 days (a count, so Poisson).
    # More failures for inactive customers and for those who churned:
    # repeated failures frustrate customers and push them away.
    lam = 0.6 + 0.8 * (1 - active) + 1.2 * churn + 0.3 * df["uses_ussd"]
    df["failed_txn_count_90d"] = rng.poisson(lam)

    # --- Primary channel: pick one of the channels the customer uses.
    def pick_channel(row):
        options, w = [], []
        if row.uses_mobile_app:       options.append("mobile_app");       w.append(4)
        if row.uses_ussd:             options.append("ussd");             w.append(3)
        if row.momo_linked:           options.append("momo");             w.append(2)
        if row.uses_internet_banking: options.append("internet_banking"); w.append(1)
        if not options:
            return "branch"            # no digital channel at all
        w = np.array(w) / sum(w)
        return rng.choice(options, p=w)

    df["primary_channel"] = df.apply(pick_channel, axis=1)
    return df


# ---------------------------------------------------------------------
# 2b. Generate the SYNTHETIC 360-degree ACTIVITY columns
# ---------------------------------------------------------------------
def add_activity_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    These describe what the customer did during a 90-day OBSERVATION WINDOW
    that ends BEFORE we know whether they churned.

    DATA LEAKAGE WARNING (important lesson):
    If 'days_since_last_txn' were measured AFTER a customer closed their
    account, it would basically BE the answer ("they left, so they stopped
    transacting"). The model would look brilliant in training and be useless
    in real life. So here churn only nudges behaviour slightly (a customer
    drifting away does a bit less), and recency is computed from activity
    counts - never directly from the churn label.
    """
    n = len(df)
    age = df["age"].to_numpy()
    active = df["active_member"].to_numpy()
    churn = df["churn"].to_numpy()
    salary_k = df["estimated_salary"].to_numpy() / 1000
    app = df["uses_mobile_app"].to_numpy()

    # --- Salary credited into this account: a strong loyalty anchor in Ghana.
    p_sal = sigmoid(0.2 + 0.6 * active + 0.008 * (salary_k - 100) - 0.5 * churn)
    df["salary_credited"] = (rng.random(n) < p_sal).astype(int)

    # 'drift' < 1 means the customer is doing less than usual.
    drift = np.where(churn == 1, 0.85, 1.0) * np.where(active == 1, 1.0, 0.6)

    # --- Branch walk-ins: older customers and non-app users visit more.
    lam_branch = (0.8 + 0.03 * np.clip(age - 30, 0, None) + 1.2 * (1 - app)) * drift
    df["branch_visits_90d"] = rng.poisson(lam_branch)

    # --- ATM withdrawals: more when salary lands in the account.
    lam_atm = (1.5 + 3.0 * df["salary_credited"].to_numpy()) * drift
    df["atm_withdrawals_90d"] = rng.poisson(lam_atm)

    # --- Digital transactions from the channels the customer uses.
    digital = (app * rng.poisson(9 * drift)
               + df["uses_ussd"].to_numpy() * rng.poisson(5 * drift)
               + df["momo_linked"].to_numpy() * rng.poisson(4 * drift)
               + df["uses_internet_banking"].to_numpy() * rng.poisson(2 * drift))

    # --- Total activity across EVERY channel (the 360 view).
    total = df["branch_visits_90d"].to_numpy() + df["atm_withdrawals_90d"].to_numpy() + digital
    df["total_txn_count_90d"] = total

    # --- Days since last transaction: the more someone transacts, the more
    # recently they probably did something. No activity in 90 days -> 91-400.
    recency = np.where(
        total > 0,
        np.clip(rng.exponential(90 / (total + 1)), 0, 90),
        rng.integers(91, 401, n),
    )
    df["days_since_last_txn"] = recency.round().astype(int)

    # --- Complaints / service calls: failed transactions cause complaints.
    lam_comp = 0.15 + 0.12 * df["failed_txn_count_90d"].to_numpy() + 0.15 * churn
    df["complaints_90d"] = rng.poisson(lam_comp)

    # --- Balance change over 3 months (%): drifting customers drain balances.
    bal_chg = rng.normal(2 - 4 * churn - 3 * (1 - active), 15)
    bal_chg = np.where(df["balance"].to_numpy() > 0, bal_chg, 0.0)   # empty accounts: no change
    df["balance_change_3m_pct"] = np.clip(bal_chg, -100, 100).round(1)
    return df


# ---------------------------------------------------------------------
# 3. Insert into Neon
# ---------------------------------------------------------------------
INSERT_COLUMNS = EXPECTED + [
    "region", "uses_mobile_app", "uses_ussd", "uses_internet_banking",
    "momo_linked", "primary_channel", "failed_txn_count_90d",
    # 360 activity view
    "days_since_last_txn", "branch_visits_90d", "atm_withdrawals_90d",
    "total_txn_count_90d", "salary_credited", "complaints_90d", "balance_change_3m_pct",
]


def insert_rows(df: pd.DataFrame) -> int:
    # Convert numpy types to plain Python types that psycopg2 understands.
    rows = [
        tuple(v.item() if hasattr(v, "item") else v for v in rec)
        + ("kaggle", True)                        # data_source, ghana_features_synthetic
        for rec in df[INSERT_COLUMNS].itertuples(index=False)
    ]

    sql = f"""
        INSERT INTO bank_customers ({", ".join(INSERT_COLUMNS)}, data_source, ghana_features_synthetic)
        VALUES %s
        ON CONFLICT (customer_id) DO UPDATE SET   -- "upsert": refresh customers already loaded
            {", ".join(f"{c} = EXCLUDED.{c}" for c in INSERT_COLUMNS if c != "customer_id")}
    """

    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn, conn.cursor() as cur:          # 'with conn' = commit on success, rollback on error
            cur.execute("SELECT COUNT(*) FROM bank_customers")
            before = cur.fetchone()[0]
            # page_size=1000 sends rows in batches of 1,000 (10 round trips for 10k rows)
            execute_values(cur, sql, rows, page_size=1000)
            cur.execute("SELECT COUNT(*) FROM bank_customers")
            after = cur.fetchone()[0]
    finally:
        conn.close()
    return after - before


# ---------------------------------------------------------------------
# 4. Summary - so you can see what the data looks like
# ---------------------------------------------------------------------
def print_summary(df: pd.DataFrame):
    print("\n=== Summary of the loaded data ===")
    print(f"Overall churn rate: {df['churn'].mean():.1%}")

    print("\nChurn rate by mobile app use (0 = no, 1 = yes):")
    print(df.groupby("uses_mobile_app")["churn"].mean().map("{:.1%}".format).to_string())

    print("\nChurn rate by MoMo link (0 = no, 1 = yes):")
    print(df.groupby("momo_linked")["churn"].mean().map("{:.1%}".format).to_string())

    print("\nAverage failed transactions (90 days) by churn (0 = stayed, 1 = left):")
    print(df.groupby("churn")["failed_txn_count_90d"].mean().round(2).to_string())

    print("\nChurn rate by salary credited (0 = no, 1 = yes):")
    print(df.groupby("salary_credited")["churn"].mean().map("{:.1%}".format).to_string())

    print("\n360 activity, average by churn (0 = stayed, 1 = left):")
    cols = ["branch_visits_90d", "atm_withdrawals_90d", "total_txn_count_90d",
            "days_since_last_txn", "complaints_90d", "balance_change_3m_pct"]
    print(df.groupby("churn")[cols].mean().round(1).T.to_string())

    print("\nPrimary channel share:")
    print(df["primary_channel"].value_counts(normalize=True).map("{:.1%}".format).to_string())


# ---------------------------------------------------------------------
# Run everything
# ---------------------------------------------------------------------
if __name__ == "__main__":
    data = load_kaggle_csv()
    data = add_ghana_features(data)
    data = add_activity_features(data)

    data.to_csv(CSV_OUT, index=False)
    print(f"  Saved enriched copy to dataset/{CSV_OUT.name}")

    print("Inserting into Neon (may take ~10-30 seconds) ...")
    added = insert_rows(data)
    print(f"  {added:,} new rows inserted, {len(data) - added:,} existing rows updated")

    print_summary(data)
    print("\nPhase 2 complete.")
