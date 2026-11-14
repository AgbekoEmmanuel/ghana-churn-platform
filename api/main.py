"""
Phase 6a - api/main.py  (the backend that powers the web platform)
==================================================================
A FastAPI web server. The HTML/CSS/JavaScript front end (Phase 6b) will ask
it for data every few seconds, so the screens update live.

Endpoints (all return JSON):
  GET  /api/health                 is the server + database OK?
  GET  /api/summary                headline KPIs (customers, high risk, avg risk...)
  GET  /api/customers              the risk table (filter, search, paging)
  GET  /api/customers/overview     analytics of the whole customer base (?scope=live|historical|all)
  GET  /api/customers/{id}         the customer 360 view + personal details + warning signs
  GET  /api/trends                 arrivals and average risk over time (live chart)
  GET  /api/breakdown/{dimension}  risk by region / primary_channel / country / products
  GET  /api/model                  model name, version, test-set metrics (churn + dormancy)
  GET  /api/dormancy/summary        dormant now, idle balances, customers going dormant
  GET  /api/dormancy/customers      ?status=dormant (already) or ?status=going (at risk)
  GET  /api/dormancy/drivers        the factors that push customers into dormancy
  GET  /api/dormancy/idle-buckets   how long customers have been idle
  POST /api/score                  "what-if": score a customer you type in

Run from the project folder (venv active):
    uvicorn api.main:app --reload
Then open http://127.0.0.1:8000/docs to try every endpoint in your browser.
"""

import os
import threading
import time
from contextlib import contextmanager
from datetime import date
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
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .profile import GENDER_SQL, build_profile, list_identity

PROJECT_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = PROJECT_DIR / "models" / "churn_model.pkl"
DORMANCY_PATH = PROJECT_DIR / "models" / "dormancy_model.pkl"
DORMANT_AFTER_DAYS = 90                       # business rule: 3 months with no activity
WEB_DIR = PROJECT_DIR / "web"
load_dotenv(PROJECT_DIR / ".env")
DATABASE_URL = os.getenv("DATABASE_URL")

app = FastAPI(title="Pulse360 API", version="1.1")

# CORS lets a web page opened from another address (e.g. VS Code Live Server
# on port 5500) call this API. Fine for local development.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
# Compress bigger JSON answers (the customer-base overview shrinks ~5x), so pages load faster.
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ---------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------
# A connection POOL keeps a few connections open and re-uses them,
# which is much faster than connecting to Neon on every request.
_pool = None
_pool_lock = threading.Lock()


# Fail fast instead of hanging when the network to Neon drops: give up on a new
# connection after 10 s, and let TCP keepalives notice a dead one within ~1 minute
# (the retry in query() then opens a fresh connection).
CONNECT_ARGS = dict(connect_timeout=10, keepalives=1, keepalives_idle=30,
                    keepalives_interval=10, keepalives_count=3)


def get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL, **CONNECT_ARGS)
    return _pool


# Neon goes to sleep after ~5 idle minutes and silently closes our open
# connections. Without care, the first requests after that fail and the page
# shows "API not reachable". So: throw away dead connections and retry.
DB_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


@contextmanager
def db_cursor():
    """Borrow a connection, give back a cursor that returns dicts, then return it."""
    pool = get_pool()
    conn = pool.getconn()
    if conn.closed:                       # already dead: swap it for a fresh one
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    broken = False
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
    except DB_ERRORS:
        broken = True          # e.g. Neon closed an idle connection
        raise
    finally:
        pool.putconn(conn, close=broken or bool(conn.closed))


def query(sql: str, params=None, attempts: int = 4):
    """Run a SELECT and return a list of dicts.
    Retries a few times, because after Neon wakes up several pooled
    connections can be dead at once (each retry discards one)."""
    for attempt in range(attempts):
        try:
            with db_cursor() as cur:
                cur.execute(sql, params)
                return [clean(r) for r in cur.fetchall()]
        except DB_ERRORS:
            if attempt == attempts - 1:
                raise HTTPException(503, "Database is waking up, try again in a few seconds")
            time.sleep(0.5 * (attempt + 1))   # give Neon a moment to wake


def page_query(sql: str, params: list, limit: int, offset: int, where_sql: str):
    """One page of rows AND the total match count in a single round trip
    (the SQL adds COUNT(*) OVER () AS total_count). Only a page past the end
    needs a second, separate count."""
    rows = query(sql, params + [limit, offset])
    if rows:
        total = rows[0]["total_count"]
    elif offset:
        total = query(f"SELECT COUNT(*) AS n FROM bank_customers WHERE {where_sql}", params)[0]["n"]
    else:
        total = 0
    for r in rows:
        r.pop("total_count", None)
    return rows, total


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


_dormancy = {"mtime": None, "bundle": None}


def get_dormancy_model():
    """The dormancy model is optional: returns None until it has been trained."""
    if not DORMANCY_PATH.exists():
        return None
    mtime = DORMANCY_PATH.stat().st_mtime
    if mtime != _dormancy["mtime"]:
        _dormancy["bundle"] = joblib.load(DORMANCY_PATH)
        _dormancy["mtime"] = mtime
    return _dormancy["bundle"]


# ---------------------------------------------------------------------
# Business rules shown on the platform
# ---------------------------------------------------------------------
def lifecycle_stage(c: dict) -> str:
    """
    One label a branch manager can read at a glance:
      Dormant          no activity of any kind for 90+ days (the bank's rule)
      Likely to churn  churn model says high risk
      Going dormant    dormancy model says high risk of going dormant in 90 days
      Slowing          quiet for 30+ days or very few transactions
      Active           everything looks normal
    """
    days = c.get("days_since_last_txn") or 0
    txns = c.get("total_txn_count_90d") or 0
    if days >= DORMANT_AFTER_DAYS:
        return "Dormant"
    if c.get("risk_band") == "high":
        return "Likely to churn"
    if c.get("dormancy_band") == "high":
        return "Going dormant"
    if days > 30 or txns < 6:
        return "Slowing"
    return "Active"


def dormancy_reasons(c: dict, norms: dict) -> list:
    """Plain-English reasons a customer is (or may become) dormant."""
    reasons = []
    days = c.get("days_since_last_txn") or 0
    if days >= DORMANT_AFTER_DAYS:
        reasons.append(f"Dormant: no activity of any kind for {days} days "
                       f"({days - DORMANT_AFTER_DAYS} days past the 90-day rule)")
    elif days > 30:
        reasons.append(f"Quiet for {days} days: dormant in {DORMANT_AFTER_DAYS - days} more days if nothing happens")
    if (c.get("total_txn_count_90d") or 0) < norms["total_txn_count_90d"] / 2:
        reasons.append(f"Low activity: {c.get('total_txn_count_90d')} transactions in 90 days "
                       f"(typical: {norms['total_txn_count_90d']:.0f})")
    if c.get("salary_credited") == 0:
        reasons.append("No salary coming into the account")
    if c.get("uses_mobile_app") == 0 and c.get("momo_linked") == 0:
        reasons.append("No mobile app and no MoMo link, so no easy way to transact")
    if c.get("active_member") == 0:
        reasons.append("Flagged as an inactive member")
    if (c.get("balance_change_3m_pct") or 0) <= -10:
        reasons.append(f"Balance fell {abs(c['balance_change_3m_pct']):.0f}% in 3 months")
    return reasons


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
@app.get("/api/ping")
def ping():
    """Keep-alive for the GitHub job that stops Render's free plan from sleeping.
    Deliberately does NOT touch the database, so Neon can still nap (saves its free hours)."""
    return {"status": "awake"}


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


# WHEN DID THE CUSTOMER JOIN THE BANK?
# The Kaggle data only gives 'tenure' = whole years with the bank (0-10).
# We turn that into an approximate join date: the day they arrived on the
# platform, minus their tenure in years, minus a fixed 0-364 day offset
# (from their customer_id) so customers with the same tenure don't all share
# one date. Approximate and synthetic, but stable: it never changes on refresh.
JOINED_SQL = """(created_at - make_interval(years => tenure)
                 - make_interval(days => mod(customer_id, 365)::int))::date"""

LIST_COLUMNS = f"""customer_id, age, gender, region, country, primary_channel, products_number,
                  balance, total_txn_count_90d, days_since_last_txn, failed_txn_count_90d,
                  complaints_90d, churn_probability, risk_band, created_at, scored_at,
                  dormancy_status, dormancy_probability, dormancy_band, tenure,
                  {JOINED_SQL} AS joined_on"""


@app.get("/api/customers")
def customers(
    risk: Optional[Literal["high", "medium", "low", "unscored"]] = None,
    region: Optional[str] = None,
    search: Optional[str] = Query(None, description="part of a customer_id"),
    sort: Literal["risk", "newest", "joined_recent", "joined_oldest"] = "risk",
    joined_from: Optional[date] = Query(None, description="joined the bank on or after (YYYY-MM-DD)"),
    joined_to: Optional[date] = Query(None, description="joined the bank on or before (YYYY-MM-DD)"),
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
    if joined_from:
        where.append(f"{JOINED_SQL} >= %s")
        params.append(joined_from)
    if joined_to:
        where.append(f"{JOINED_SQL} <= %s")
        params.append(joined_to)
    order = {
        "risk": "churn_probability DESC NULLS LAST, created_at DESC",
        "newest": "created_at DESC",
        "joined_recent": "joined_on DESC, customer_id",
        "joined_oldest": "joined_on ASC, customer_id",
    }[sort]
    where_sql = " AND ".join(where)

    rows, total = page_query(f"""
        SELECT {LIST_COLUMNS}, COUNT(*) OVER () AS total_count FROM bank_customers
        WHERE {where_sql} ORDER BY {order} LIMIT %s OFFSET %s
    """, params, limit, offset, where_sql)
    for r in rows:
        r["lifecycle_stage"] = lifecycle_stage(r)
        r.update(list_identity(r))          # synthetic name / phone, stable per customer
    return {"total": total, "limit": limit, "offset": offset, "customers": rows}


# ---------------------------------------------------------------------
# Customer base overview (analytics page)
# ---------------------------------------------------------------------
SCOPES = {
    "live": "data_source = 'simulator'",        # customers on the platform now
    "historical": "data_source = 'kaggle'",     # the 10,000-customer history (includes who churned)
    "all": "TRUE",
}
STATUS_ORDER = ["Active", "Slowing", "Going dormant", "Likely to churn", "Dormant", "Churned"]
# Same rules as lifecycle_stage(), plus "Churned" for history rows where we know the outcome.
STATUS_SQL = """(CASE WHEN churn = 1 THEN 'Churned'
                      WHEN days_since_last_txn >= 90 THEN 'Dormant'
                      WHEN risk_band = 'high' THEN 'Likely to churn'
                      WHEN dormancy_band = 'high' THEN 'Going dormant'
                      WHEN days_since_last_txn > 30 OR total_txn_count_90d < 6 THEN 'Slowing'
                      ELSE 'Active' END)"""
AGE_BAND_SQL = """(CASE WHEN age < 25 THEN '18–24' WHEN age < 35 THEN '25–34' WHEN age < 45 THEN '35–44'
                        WHEN age < 55 THEN '45–54' WHEN age < 65 THEN '55–64' ELSE '65+' END)"""
# Each breakdown: (label expression, sort expression). All run over the same "base" rows.
DIMENSIONS = {
    "age": (AGE_BAND_SQL, "MIN(age)"),
    "gender": ("g", "MIN(CASE g WHEN 'Male' THEN 1 ELSE 2 END)"),
    "income": ("""(CASE WHEN monthly_income < 1500 THEN 'Under GH₵ 1,500'
                        WHEN monthly_income < 3000 THEN 'GH₵ 1,500 – 3,000'
                        WHEN monthly_income < 5000 THEN 'GH₵ 3,000 – 5,000'
                        WHEN monthly_income < 10000 THEN 'GH₵ 5,000 – 10,000'
                        WHEN monthly_income < 15000 THEN 'GH₵ 10,000 – 15,000'
                        ELSE 'GH₵ 15,000+' END)""", "MIN(monthly_income)"),
    "balance": ("""(CASE WHEN balance = 0 THEN 'Zero balance'
                         WHEN balance < 25000 THEN 'Under GH₵ 25k'
                         WHEN balance < 75000 THEN 'GH₵ 25k – 75k'
                         WHEN balance < 125000 THEN 'GH₵ 75k – 125k'
                         WHEN balance < 175000 THEN 'GH₵ 125k – 175k'
                         ELSE 'GH₵ 175k+' END)""", "MIN(balance)"),
    "credit": ("""(CASE WHEN credit_score < 580 THEN 'Poor (<580)'
                        WHEN credit_score < 670 THEN 'Fair (580–669)'
                        WHEN credit_score < 740 THEN 'Good (670–739)'
                        WHEN credit_score < 800 THEN 'Very good (740–799)'
                        ELSE 'Excellent (800+)' END)""", "MIN(credit_score)"),
    "region": ("region", "-COUNT(*)"),
    "tenure": ("tenure::text", "MIN(tenure)"),
    "products": ("products_number::text", "MIN(products_number)"),
    "channel": ("primary_channel", "-COUNT(*)"),
    "status": ("status", "MIN(array_position(ARRAY['" + "','".join(STATUS_ORDER) + "'], status))"),
}


# The customer mix changes slowly (a few customers per simulator run), so the
# overview is kept for a minute instead of re-asking the database on every view.
OVERVIEW_TTL_S = 60
_overview_cache: dict = {}


@app.get("/api/customers/overview")
def customers_overview(scope: Literal["live", "historical", "all"] = "live"):
    """Everything about the customer base in one call: headline numbers and
    breakdowns by age, gender, income tier, balance, credit score, region,
    tenure, products, channel and status."""
    cached = _overview_cache.get(scope)
    if cached and time.time() - cached[0] < OVERVIEW_TTL_S:
        return cached[1]

    base = f"""WITH base AS (
        SELECT *, {GENDER_SQL} AS g, estimated_salary / 12.0 AS monthly_income, {STATUS_SQL} AS status
        FROM bank_customers WHERE {SCOPES[scope]})"""

    kpi_sql = """
        SELECT COUNT(*) AS customers,
               COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '30 days')  AS new_30d,
               ROUND(AVG(age), 1) AS avg_age,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY age) AS median_age,
               COALESCE(SUM(balance), 0) AS total_balance,
               ROUND(AVG(balance), 2) AS avg_balance,
               COUNT(*) FILTER (WHERE balance = 0) AS zero_balance,
               ROUND(AVG(monthly_income), 2) AS avg_income,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY monthly_income) AS median_income,
               ROUND(AVG(tenure), 2) AS avg_tenure,
               ROUND(AVG(products_number), 2) AS avg_products,
               ROUND(AVG(credit_score)) AS avg_credit_score,
               COUNT(*) FILTER (WHERE g = 'Male') AS male,
               COUNT(*) FILTER (WHERE g = 'Female') AS female,
               COUNT(*) FILTER (WHERE uses_mobile_app = 1 OR uses_ussd = 1
                                   OR uses_internet_banking = 1 OR momo_linked = 1) AS digital,
               COALESCE(SUM(uses_mobile_app), 0) AS uses_app,
               COALESCE(SUM(uses_ussd), 0) AS uses_ussd,
               COALESCE(SUM(uses_internet_banking), 0) AS uses_internet_banking,
               COALESCE(SUM(momo_linked), 0) AS momo_linked,
               COALESCE(SUM(credit_card), 0) AS credit_card,
               COALESCE(SUM(salary_credited), 0) AS salary_credited,
               COALESCE(SUM(active_member), 0) AS active_member,
               ROUND(AVG(churn_probability), 4) AS avg_churn_probability,
               COUNT(*) FILTER (WHERE churn IS NOT NULL) AS with_outcome
        FROM base"""

    union = " UNION ALL ".join(
        f"""SELECT '{name}' AS dim, ({label})::text AS label, ({sort})::numeric AS sort_key,
                   COUNT(*) AS customers,
                   ROUND(AVG(churn_probability), 4) AS avg_churn_probability,
                   ROUND(AVG((days_since_last_txn >= 90)::int), 4) AS dormant_rate,
                   ROUND(AVG(churn), 4) AS churn_rate,
                   ROUND(AVG(balance), 2) AS avg_balance,
                   ROUND(AVG(monthly_income), 2) AS avg_income,
                   ROUND(AVG(age), 1) AS avg_age
            FROM base GROUP BY 2"""
        for name, (label, sort) in DIMENSIONS.items())
    # Status mix inside each age band (stacked bar on the page)
    age_status_sql = f"SELECT {AGE_BAND_SQL} AS age_band, status, COUNT(*) AS customers FROM base GROUP BY 1, 2"

    # ONE round trip to the database: the three result sets come back as JSON.
    # (The SQL itself takes milliseconds; the network hop to Neon is the slow part.)
    row = query(base + f"""
        SELECT (SELECT row_to_json(k) FROM ({kpi_sql}) k)                         AS kpis,
               (SELECT json_agg(d ORDER BY d.dim, d.sort_key) FROM ({union}) d)   AS dims,
               (SELECT json_agg(a ORDER BY a.age_band) FROM ({age_status_sql}) a) AS age_status""")[0]
    k = row["kpis"]
    n = k["customers"] or 0
    dims = {name: [] for name in DIMENSIONS}
    for r in row["dims"] or []:
        dim = r.pop("dim")
        r.pop("sort_key")
        r["share"] = round(r["customers"] / n, 4) if n else 0
        dims[dim].append(r)
    age_status = row["age_status"] or []

    status_counts = {s["label"]: s["customers"] for s in dims["status"]}
    result = {
        "scope": scope,
        "kpis": k | {"status": {s: status_counts.get(s, 0) for s in STATUS_ORDER}},
        "dimensions": dims,
        "age_status": age_status,
        "status_order": STATUS_ORDER,
        "notes": {
            "income": "Monthly income = the Kaggle 'estimated salary', read as GH₵ per year, divided by 12.",
            "gender": "Older simulated customers have no stored gender; a stable synthetic one is used.",
            "churned": "Only the historical book knows who actually churned; live customers show risk instead.",
        },
    }
    _overview_cache[scope] = (time.time(), result)
    return result


@app.get("/api/customers/{customer_id}")
def customer_360(customer_id: int):
    rows = query(f"SELECT *, {JOINED_SQL} AS joined_on FROM bank_customers WHERE customer_id = %s", [customer_id])
    if not rows:
        raise HTTPException(404, "Customer not found")
    c = rows[0]
    # Hide columns that must never reach the screen: the training label and the
    # simulator's hidden "future" (that would be cheating).
    for hidden in ("went_dormant_90d", "sim_activity_prob", "sim_goes_dormant", "churn"):
        c.pop(hidden, None)
    # Personal / KYC details (synthetic, stable per customer). Gender is shown on
    # the profile only; the models never use it.
    c["profile"] = build_profile(c)
    c["gender"] = c["profile"]["gender"]
    c["dormancy_reasons"] = dormancy_reasons(c, get_norms())
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
    keys = ("model_name", "model_version", "threshold", "metrics", "trained_at", "features")
    b = get_model()
    out = {k: b[k] for k in keys}
    d = get_dormancy_model()
    out["dormancy"] = {k: d[k] for k in keys + ("drivers",)} if d else None
    return out


# ---------------------------------------------------------------------
# Dormancy (Phase 8)
# ---------------------------------------------------------------------
@app.get("/api/dormancy/summary")
def dormancy_summary():
    return query("""
        SELECT COUNT(*) FILTER (WHERE days_since_last_txn >= 90)                    AS dormant,
               COALESCE(SUM(balance) FILTER (WHERE days_since_last_txn >= 90), 0)   AS dormant_balance,
               ROUND(AVG(days_since_last_txn) FILTER (WHERE days_since_last_txn >= 90)) AS avg_days_idle_dormant,
               COUNT(*) FILTER (WHERE days_since_last_txn < 90)                     AS active,
               COUNT(*) FILTER (WHERE dormancy_band = 'high')                       AS going_dormant_high,
               COUNT(*) FILTER (WHERE dormancy_band = 'medium')                     AS going_dormant_medium,
               COALESCE(SUM(balance) FILTER (WHERE dormancy_band = 'high'), 0)      AS going_dormant_balance,
               ROUND(AVG(dormancy_probability), 4)                                  AS avg_dormancy_probability,
               COUNT(*)                                                             AS live_customers
        FROM bank_customers
        WHERE data_source = 'simulator'
    """)[0] | {"model_trained": get_dormancy_model() is not None}


@app.get("/api/dormancy/customers")
def dormancy_customers(
    status: Literal["dormant", "going"] = "going",
    region: Optional[str] = None,
    limit: int = Query(25, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """status=dormant: already dormant, longest idle first.
       status=going: still active, highest chance of going dormant first."""
    where, params = ["data_source = 'simulator'"], []
    if status == "dormant":
        where.append("days_since_last_txn >= 90")
        order = "days_since_last_txn DESC, balance DESC"
    else:
        where.append("days_since_last_txn < 90 AND dormancy_probability IS NOT NULL")
        order = "dormancy_probability DESC"
    if region:
        where.append("region = %s")
        params.append(region)
    where_sql = " AND ".join(where)
    rows, total = page_query(f"""
        SELECT {LIST_COLUMNS}, salary_credited, uses_mobile_app, momo_linked, active_member,
               balance_change_3m_pct, COUNT(*) OVER () AS total_count
        FROM bank_customers WHERE {where_sql} ORDER BY {order} LIMIT %s OFFSET %s
    """, params, limit, offset, where_sql)
    norms = get_norms()
    for r in rows:
        r["lifecycle_stage"] = lifecycle_stage(r)
        r["dormancy_reasons"] = dormancy_reasons(r, norms)
        r.update(list_identity(r))
    return {"total": total, "limit": limit, "offset": offset, "customers": rows}


@app.get("/api/dormancy/drivers")
def dormancy_drivers():
    d = get_dormancy_model()
    if not d:
        raise HTTPException(404, "Dormancy model not trained yet. Run scripts/train_dormancy_model.py")
    return d["drivers"]


@app.get("/api/dormancy/idle-buckets")
def idle_buckets():
    """How long live customers have been idle, in bands (for a bar chart)."""
    return query("""
        SELECT label, COUNT(b.id) AS customers
        FROM (VALUES (1, '0-7 days', 0, 7), (2, '8-30 days', 8, 30), (3, '31-60 days', 31, 60),
                     (4, '61-89 days', 61, 89), (5, '90-180 days (dormant)', 90, 180),
                     (6, '181+ days (dormant)', 181, 100000)) AS v(ord, label, lo, hi)
        LEFT JOIN bank_customers b
               ON b.data_source = 'simulator' AND b.days_since_last_txn BETWEEN v.lo AND v.hi
        GROUP BY v.ord, label ORDER BY v.ord
    """)


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

    # Dormancy: rule first (already dormant?), then the model for active customers.
    d = get_dormancy_model()
    d_prob = d_band = None
    if row["days_since_last_txn"] < DORMANT_AFTER_DAYS and d:
        d_prob = float(d["pipeline"].predict_proba(pd.DataFrame([row])[d["features"]])[:, 1][0])
        dt = d["threshold"]
        d_band = "high" if d_prob >= dt else "medium" if d_prob >= dt / 2 else "low"
    row["dormancy_band"] = d_band
    norms = get_norms()
    return {"churn_probability": round(p, 4), "risk_band": band,
            "dormancy_status": "dormant" if row["days_since_last_txn"] >= DORMANT_AFTER_DAYS else "active",
            "dormancy_probability": None if d_prob is None else round(d_prob, 4),
            "dormancy_band": d_band,
            "lifecycle_stage": lifecycle_stage(row),
            "warning_signs": warning_signs(row, norms),
            "dormancy_reasons": dormancy_reasons(row, norms),
            "model_version": b["model_version"]}


# ---------------------------------------------------------------------
# Warm-up: as soon as the server starts, connect to Neon (waking it if it
# was asleep), load both models and pre-compute the customer overview, in the
# background. The first visitor then gets answers immediately instead of
# paying for all of that on their first click.
# ---------------------------------------------------------------------
def _warm_up():
    steps = [
        ("database", lambda: query("SELECT 1 AS ok")),
        ("churn model", get_model),
        ("dormancy model", get_dormancy_model),
        ("norms", get_norms),
        ("overview", lambda: customers_overview("live")),
        ("summary", summary),
    ]
    for name, step in steps:
        try:
            step()
        except Exception as e:                      # never stop the server over a warm-up step
            print(f"Warm-up: {name} skipped ({str(e)[:80]})")
    print("Warm-up finished: database connected, models loaded.")


@app.on_event("startup")
def start_warm_up():
    threading.Thread(target=_warm_up, daemon=True).start()


# ---------------------------------------------------------------------
# Serve the front end (Phase 6b) from the same server, if the web/ folder exists.
# Must come LAST so it doesn't hide the /api routes.
# ---------------------------------------------------------------------
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
