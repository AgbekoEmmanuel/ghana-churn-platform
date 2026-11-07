"""
Phase 6a - api/main.py  (the backend that powers the web platform)
==================================================================
A FastAPI web server. The HTML/CSS/JavaScript front end (Phase 6b) will ask
it for data every few seconds, so the screens update live.

Endpoints (all return JSON):
  GET  /api/health                 is the server + database OK?
  GET  /api/summary                headline KPIs (customers, high risk, avg risk...)
  GET  /api/customers              the risk table (filter, search, paging)
  GET  /api/customers/{id}         the customer 360 view + warning signs
  GET  /api/trends                 arrivals and average risk over time (live chart)
  GET  /api/breakdown/{dimension}  risk by region / primary_channel / country / products
  GET  /api/model                  model name, version, test-set metrics
  POST /api/score                  "what-if": score a customer you type in

Run from the project folder (venv active):
    uvicorn api.main:app --reload
Then open http://127.0.0.1:8000/docs to try every endpoint in your browser.
"""

import os
import threading
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

import joblib
import pandas as pd
import psycopg2
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PROJECT_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = PROJECT_DIR / "models" / "churn_model.pkl"
WEB_DIR = PROJECT_DIR / "web"
load_dotenv(PROJECT_DIR / ".env")
DATABASE_URL = os.getenv("DATABASE_URL")

app = FastAPI(title="Ghana Bank Churn Platform API", version="1.0")

# CORS lets a web page opened from another address (e.g. VS Code Live Server
# on port 5500) call this API. Fine for local development.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------
# A connection POOL keeps a few connections open and re-uses them,
# which is much faster than connecting to Neon on every request.
_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL)
    return _pool


@contextmanager
def db_cursor():
    """Borrow a connection, give back a cursor that returns dicts, then return it."""
    pool = get_pool()
    conn = pool.getconn()
    broken = False
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
    except psycopg2.OperationalError:
        broken = True          # e.g. Neon closed an idle connection
        raise
    finally:
        pool.putconn(conn, close=broken)


def query(sql: str, params=None, retry: bool = True):
    """Run a SELECT and return a list of dicts. Retries once if the connection dropped."""
    try:
        with db_cursor() as cur:
            cur.execute(sql, params)
            return [clean(r) for r in cur.fetchall()]
    except psycopg2.OperationalError:
        if retry:
            return query(sql, params, retry=False)
        raise HTTPException(503, "Database unavailable, try again shortly")


def clean(row: dict) -> dict:
    """Postgres NUMERIC comes back as Decimal, which JSON can't handle; make it float."""
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


# ---------------------------------------------------------------------
# Model (loaded once, reloaded automatically if you retrain)
# ---------------------------------------------------------------------
_model = {"mtime": None, "bundle": None}


def get_model():
    if not MODEL_PATH.exists():
        raise HTTPException(500, "Model file not found. Run scripts/train_model.py first.")
    mtime = MODEL_PATH.stat().st_mtime
    if mtime != _model["mtime"]:
        _model["bundle"] = joblib.load(MODEL_PATH)
        _model["mtime"] = mtime
    return _model["bundle"]


# ---------------------------------------------------------------------
# Business rules shown on the platform
# ---------------------------------------------------------------------
def lifecycle_stage(c: dict) -> str:
    """
    Dormancy is the EARLY warning, churn is the end result.
    Active -> Slowing -> Dormant risk -> Likely to churn
    """
    if c.get("risk_band") == "high":
        return "Likely to churn"
    days = c.get("days_since_last_txn") or 0
    txns = c.get("total_txn_count_90d") or 0
    if days > 60:
        return "Dormant risk"
    if days > 30 or txns < 6:
        return "Slowing"
    return "Active"


def warning_signs(c: dict, norms: dict) -> list:
    """
    Plain-English reasons a relationship manager can act on. Each customer is
    compared with the typical (median) customer who STAYED in the Kaggle data.
    """
    signs = []
    if (c.get("failed_txn_count_90d") or 0) >= norms["failed_txn_count_90d"] + 2:
        signs.append(f"{c['failed_txn_count_90d']} failed transactions in 90 days")
    if (c.get("complaints_90d") or 0) >= 2:
        signs.append(f"{c['complaints_90d']} complaints or service calls in 90 days")
    if (c.get("days_since_last_txn") or 0) > 30:
        signs.append(f"No activity for {c['days_since_last_txn']} days")
    if (c.get("total_txn_count_90d") or 0) < norms["total_txn_count_90d"] / 2:
        signs.append(f"Low activity: {c['total_txn_count_90d']} transactions in 90 days "
                     f"(typical: {norms['total_txn_count_90d']:.0f})")
    if (c.get("balance_change_3m_pct") or 0) <= -10:
        signs.append(f"Balance fell {abs(c['balance_change_3m_pct']):.0f}% in 3 months")
    if c.get("salary_credited") == 0:
        signs.append("Salary is not paid into this account")
    if (c.get("products_number") or 0) >= 3:
        signs.append(f"Holds {c['products_number']} products (a high-churn group in the data)")
    if c.get("active_member") == 0:
        signs.append("Flagged as an inactive member")
    if c.get("momo_linked") == 0 and c.get("uses_mobile_app") == 0:
        signs.append("No MoMo link and no mobile app: weak digital ties")
    return signs


_norms_cache = {}


def get_norms():
    if not _norms_cache:
        rows = query("""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY failed_txn_count_90d) AS failed_txn_count_90d,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY total_txn_count_90d)  AS total_txn_count_90d
            FROM bank_customers WHERE data_source = 'kaggle' AND churn = 0
        """)
        _norms_cache.update({k: float(v or 0) for k, v in rows[0].items()})
    return _norms_cache


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------
@app.get("/api/health")
def health():
    query("SELECT 1 AS ok")
    return {"status": "ok"}


@app.get("/api/summary")
def summary():
    row = query("""
        SELECT COUNT(*)                                              AS live_customers,
               COUNT(*) FILTER (WHERE scored_at IS NOT NULL)         AS scored,
               COUNT(*) FILTER (WHERE scored_at IS NULL)             AS waiting_to_score,
               COUNT(*) FILTER (WHERE risk_band = 'high')            AS high_risk,
               COUNT(*) FILTER (WHERE risk_band = 'medium')          AS medium_risk,
               COUNT(*) FILTER (WHERE risk_band = 'low')             AS low_risk,
               ROUND(AVG(churn_probability), 4)                      AS avg_churn_probability,
               COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '1 hour') AS new_last_hour,
               COALESCE(SUM(balance) FILTER (WHERE risk_band = 'high'), 0)   AS balance_at_risk,
               MAX(created_at)                                       AS last_customer_at,
               MAX(scored_at)                                        AS last_scored_at
        FROM bank_customers
        WHERE data_source = 'simulator'
    """)[0]
    history = query("""
        SELECT COUNT(*) AS customers, ROUND(AVG(churn) * 100, 1) AS churn_rate_pct
        FROM bank_customers WHERE data_source = 'kaggle'
    """)[0]
    row["historical_customers"] = history["customers"]
    row["historical_churn_rate_pct"] = history["churn_rate_pct"]
    return row


LIST_COLUMNS = """customer_id, age, region, country, primary_channel, products_number,
                  balance, total_txn_count_90d, days_since_last_txn, failed_txn_count_90d,
                  complaints_90d, churn_probability, risk_band, created_at, scored_at"""


@app.get("/api/customers")
def customers(
    risk: Optional[Literal["high", "medium", "low", "unscored"]] = None,
    region: Optional[str] = None,
    search: Optional[str] = Query(None, description="part of a customer_id"),
    sort: Literal["risk", "newest"] = "risk",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    where, params = ["data_source = 'simulator'"], []
    if risk == "unscored":
        where.append("scored_at IS NULL")
    elif risk:
        where.append("risk_band = %s")
        params.append(risk)
    if region:
        where.append("region = %s")
        params.append(region)
    if search:
        where.append("customer_id::text LIKE %s")
        params.append(f"%{search}%")
    order = "churn_probability DESC NULLS LAST, created_at DESC" if sort == "risk" else "created_at DESC"
    where_sql = " AND ".join(where)

    total = query(f"SELECT COUNT(*) AS n FROM bank_customers WHERE {where_sql}", params)[0]["n"]
    rows = query(f"""
        SELECT {LIST_COLUMNS} FROM bank_customers
        WHERE {where_sql} ORDER BY {order} LIMIT %s OFFSET %s
    """, params + [limit, offset])
    for r in rows:
        r["lifecycle_stage"] = lifecycle_stage(r)
    return {"total": total, "limit": limit, "offset": offset, "customers": rows}


@app.get("/api/customers/{customer_id}")
def customer_360(customer_id: int):
    rows = query("SELECT * FROM bank_customers WHERE customer_id = %s", [customer_id])
    if not rows:
        raise HTTPException(404, "Customer not found")
    c = rows[0]
    c.pop("gender", None)                     # not used anywhere on the platform
    c["lifecycle_stage"] = lifecycle_stage(c)
    c["warning_signs"] = warning_signs(c, get_norms())
    c["channels"] = {
        "Mobile app": c["uses_mobile_app"], "USSD": c["uses_ussd"],
        "Internet banking": c["uses_internet_banking"], "MoMo link": c["momo_linked"],
        "Branch visits (90d)": c["branch_visits_90d"], "ATM withdrawals (90d)": c["atm_withdrawals_90d"],
    }
    return c


@app.get("/api/trends")
def trends(minutes: int = Query(720, ge=5, le=10080),
           bucket_minutes: int = Query(15, ge=1, le=1440)):
    """
    Arrivals and average risk per time bucket, for the live line chart.
    Defaults: the last 12 hours in 15-minute buckets (fits a 5-minute simulator).
    date_bin() rounds each timestamp down to the start of its bucket.
    """
    return query("""
        SELECT date_bin(make_interval(mins => %s), created_at, TIMESTAMPTZ '2000-01-01') AS bucket,
               COUNT(*)                                     AS new_customers,
               COUNT(*) FILTER (WHERE risk_band = 'high')   AS high_risk,
               ROUND(AVG(churn_probability), 4)             AS avg_churn_probability
        FROM bank_customers
        WHERE data_source = 'simulator' AND created_at > NOW() - make_interval(mins => %s)
        GROUP BY 1 ORDER BY 1
    """, [bucket_minutes, minutes])


BREAKDOWNS = {"region": "region", "primary_channel": "primary_channel",
              "country": "country", "products": "products_number"}


@app.get("/api/breakdown/{dimension}")
def breakdown(dimension: Literal["region", "primary_channel", "country", "products"]):
    col = BREAKDOWNS[dimension]               # safe: only known column names reach the SQL
    return query(f"""
        SELECT {col}::text                                  AS label,
               COUNT(*)                                     AS customers,
               COUNT(*) FILTER (WHERE risk_band = 'high')   AS high_risk,
               ROUND(AVG(churn_probability), 4)             AS avg_churn_probability
        FROM bank_customers
        WHERE data_source = 'simulator' AND scored_at IS NOT NULL
        GROUP BY 1 ORDER BY avg_churn_probability DESC NULLS LAST
    """)


@app.get("/api/model")
def model_info():
    b = get_model()
    return {k: b[k] for k in ("model_name", "model_version", "threshold", "metrics", "trained_at", "features")}


class WhatIfCustomer(BaseModel):
    """A customer typed into the 'what-if' form. Defaults = a fairly typical customer."""
    credit_score: int = Field(650, ge=300, le=900)
    country: Literal["France", "Spain", "Germany"] = "France"
    age: int = Field(38, ge=18, le=100)
    tenure: int = Field(5, ge=0, le=50)
    balance: float = Field(75000, ge=0)
    products_number: int = Field(1, ge=1, le=10)
    credit_card: int = Field(1, ge=0, le=1)
    active_member: int = Field(1, ge=0, le=1)
    estimated_salary: float = Field(100000, ge=0)
    region: str = "Greater Accra"
    uses_mobile_app: int = Field(1, ge=0, le=1)
    uses_ussd: int = Field(1, ge=0, le=1)
    uses_internet_banking: int = Field(0, ge=0, le=1)
    momo_linked: int = Field(1, ge=0, le=1)
    primary_channel: Literal["branch", "ussd", "mobile_app", "internet_banking", "momo"] = "mobile_app"
    failed_txn_count_90d: int = Field(1, ge=0)
    days_since_last_txn: int = Field(5, ge=0)
    branch_visits_90d: int = Field(1, ge=0)
    atm_withdrawals_90d: int = Field(3, ge=0)
    total_txn_count_90d: int = Field(15, ge=0)
    salary_credited: int = Field(1, ge=0, le=1)
    complaints_90d: int = Field(0, ge=0)
    balance_change_3m_pct: float = Field(0, ge=-100, le=100)


@app.post("/api/score")
def score(customer: WhatIfCustomer):
    """Score a made-up customer without saving it: great for 'what if they get a MoMo link?'"""
    b = get_model()
    row = customer.model_dump()
    df = pd.DataFrame([row])[b["features"]]
    p = float(b["pipeline"].predict_proba(df)[:, 1][0])
    t = b["threshold"]
    band = "high" if p >= t else "medium" if p >= t / 2 else "low"
    row.update({"risk_band": band})
    return {"churn_probability": round(p, 4), "risk_band": band,
            "lifecycle_stage": lifecycle_stage(row),
            "warning_signs": warning_signs(row, get_norms()),
            "model_version": b["model_version"]}


# ---------------------------------------------------------------------
# Serve the front end (Phase 6b) from the same server, if the web/ folder exists.
# Must come LAST so it doesn't hide the /api routes.
# ---------------------------------------------------------------------
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
