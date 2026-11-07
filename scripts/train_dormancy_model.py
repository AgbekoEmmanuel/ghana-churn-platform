"""
Phase 8 - train_dormancy_model.py
=================================
A SECOND model, next to the churn model. It answers:
    "Will this currently ACTIVE customer go DORMANT in the next 90 days?"
    (dormant = no activity of any kind for 90+ days)

Customers who are ALREADY dormant don't need a model; a simple rule finds
them (days_since_last_txn >= 90). This model is the EARLY WARNING for the
ones who are still active but drifting.

Same recipe as the churn model (Phase 3 + calibration), so it should feel familiar:
  1. Load the Kaggle customers that have a dormancy label (the active ones).
  2. Split 60/20/20, train Logistic Regression and XGBoost, keep the better one.
  3. Calibrate the probabilities, tune the threshold, score the test set.
  4. Work out the DRIVERS with permutation importance: which factors push
     customers into dormancy most.
  5. Save to models/dormancy_model.pkl.

The label is SYNTHETIC (see load_baseline.py, dormancy_propensity), so the
model learns OUR assumptions about dormancy, not measured Ghanaian behaviour.

Run (venv active):
    python scripts\\train_dormancy_model.py
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, brier_score_loss, confusion_matrix,
                             f1_score, precision_score, recall_score, roc_auc_score)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from xgboost import XGBClassifier

PROJECT_DIR = Path(__file__).resolve().parent.parent
MODEL_DIR = PROJECT_DIR / "models"
MODEL_DIR.mkdir(exist_ok=True)
load_dotenv(PROJECT_DIR / ".env")
DATABASE_URL = os.getenv("DATABASE_URL")
SEED = 42

# Same inputs as the churn model (gender deliberately excluded).
NUMERIC = ["credit_score", "age", "tenure", "balance", "products_number", "credit_card",
           "active_member", "estimated_salary", "uses_mobile_app", "uses_ussd",
           "uses_internet_banking", "momo_linked", "failed_txn_count_90d",
           "days_since_last_txn", "branch_visits_90d", "atm_withdrawals_90d",
           "total_txn_count_90d", "salary_credited", "complaints_90d", "balance_change_3m_pct"]
CATEGORICAL = ["country", "region", "primary_channel"]
TARGET = "went_dormant_90d"

# Friendly names for the drivers chart on the platform.
FRIENDLY = {
    "days_since_last_txn": "Days since last activity", "total_txn_count_90d": "Transactions in 90 days",
    "active_member": "Active member flag", "salary_credited": "Salary paid into account",
    "uses_mobile_app": "Uses mobile app", "momo_linked": "MoMo linked",
    "balance_change_3m_pct": "Balance trend (3 months)", "age": "Age",
    "products_number": "Number of products", "branch_visits_90d": "Branch visits",
    "atm_withdrawals_90d": "ATM withdrawals", "failed_txn_count_90d": "Failed transactions",
    "complaints_90d": "Complaints", "uses_ussd": "Uses USSD",
    "uses_internet_banking": "Uses internet banking", "balance": "Balance",
    "estimated_salary": "Salary", "credit_score": "Credit score", "tenure": "Tenure",
    "credit_card": "Has credit card", "country": "Country", "region": "Region",
    "primary_channel": "Primary channel",
}


def load_data() -> pd.DataFrame:
    cols = NUMERIC + CATEGORICAL + [TARGET]
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT {", ".join(cols)} FROM bank_customers
                WHERE data_source = 'kaggle' AND {TARGET} IS NOT NULL
            """)
            df = pd.DataFrame(cur.fetchall(), columns=cols)
    finally:
        conn.close()
    if df.empty:
        raise SystemExit("No dormancy labels found. Run load_baseline.py (v3) first.")
    for c in NUMERIC + [TARGET]:
        df[c] = pd.to_numeric(df[c])
    print(f"Loaded {len(df):,} active customers with a dormancy label. "
          f"Went dormant: {df[TARGET].mean():.1%}")
    return df


def make_pipeline(model, scale):
    pre = ColumnTransformer([
        ("num", StandardScaler() if scale else "passthrough", NUMERIC),
        ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL),
    ])
    return Pipeline([("prep", pre), ("model", model)])


def best_threshold(y, p):
    grid = np.arange(0.10, 0.91, 0.01)
    return round(float(grid[int(np.argmax([f1_score(y, p >= t) for t in grid]))]), 2)


if __name__ == "__main__":
    df = load_data()
    X, y = df.drop(columns=[TARGET]), df[TARGET]
    X_tr, X_tmp, y_tr, y_tmp = train_test_split(X, y, test_size=0.4, stratify=y, random_state=SEED)
    X_val, X_te, y_val, y_te = train_test_split(X_tmp, y_tmp, test_size=0.5, stratify=y_tmp, random_state=SEED)

    pos_weight = (y_tr == 0).sum() / (y_tr == 1).sum()
    candidates = {
        "Logistic Regression": make_pipeline(LogisticRegression(class_weight="balanced", max_iter=2000), True),
        "XGBoost": make_pipeline(XGBClassifier(
            n_estimators=400, max_depth=4, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
            scale_pos_weight=pos_weight, eval_metric="logloss", random_state=SEED, n_jobs=-1), False),
    }

    print(f"\n{'Model':<22}{'ROC-AUC':>9}{'PR-AUC':>9}")
    scores = {}
    for name, pipe in candidates.items():
        pipe.fit(X_tr, y_tr)
        p = pipe.predict_proba(X_val)[:, 1]
        scores[name] = average_precision_score(y_val, p)
        print(f"{name:<22}{roc_auc_score(y_val, p):>9.3f}{scores[name]:>9.3f}")
    best_name = max(scores, key=scores.get)
    best = candidates[best_name]
    print(f"\nWinner: {best_name}")

    # Calibrate so "0.40" means about 40% of such customers go dormant.
    try:
        from sklearn.frozen import FrozenEstimator
        calibrated = CalibratedClassifierCV(FrozenEstimator(best), method="sigmoid")
    except ImportError:
        calibrated = CalibratedClassifierCV(best, method="sigmoid", cv="prefit")
    calibrated.fit(X_val, y_val)
    threshold = best_threshold(y_val, calibrated.predict_proba(X_val)[:, 1])

    p_te = calibrated.predict_proba(X_te)[:, 1]
    pred = (p_te >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_te, pred).ravel()
    metrics = {
        "roc_auc": round(roc_auc_score(y_te, p_te), 4),
        "pr_auc": round(average_precision_score(y_te, p_te), 4),
        "precision": round(precision_score(y_te, pred), 4),
        "recall": round(recall_score(y_te, pred), 4),
        "brier": round(brier_score_loss(y_te, p_te), 4),
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }
    print(f"\n=== Test set (threshold {threshold:.2f}) ===")
    print(f"ROC-AUC {metrics['roc_auc']:.3f} | PR-AUC {metrics['pr_auc']:.3f} | "
          f"Precision {metrics['precision']:.3f} | Recall {metrics['recall']:.3f}")
    print(f"Caught {tp} customers who went dormant, missed {fn}, false alarms {fp}")

    # ---- DRIVERS: which factors matter most? ----
    # PERMUTATION IMPORTANCE: shuffle one column at a time on the validation
    # set and measure how much worse the model gets. A big drop = an important
    # driver. It works for any model and treats 'region' as ONE factor (not
    # ten one-hot columns), so the ranking is fair and easy to explain.
    from sklearn.inspection import permutation_importance
    perm = permutation_importance(best, X_val, y_val, scoring="roc_auc",
                                  n_repeats=5, random_state=SEED, n_jobs=-1)
    grouped = pd.Series(np.clip(perm.importances_mean, 0, None), index=X_val.columns)
    grouped = (grouped / grouped.sum()).sort_values(ascending=False)
    drivers = [{"feature": f, "label": FRIENDLY.get(f, f), "importance": round(float(v), 4)}
               for f, v in grouped.head(10).items()]
    print("\nTop drivers of dormancy:")
    for d in drivers:
        print(f"  {d['label']:<28} {d['importance']:.1%}")

    version = "dorm-v1-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bundle = {"pipeline": calibrated, "threshold": threshold, "features": NUMERIC + CATEGORICAL,
              "model_name": best_name + " (calibrated)", "model_version": version,
              "metrics": metrics, "drivers": drivers,
              "trained_at": datetime.now(timezone.utc).isoformat()}
    joblib.dump(bundle, MODEL_DIR / "dormancy_model.pkl")
    (MODEL_DIR / "dormancy_metrics.json").write_text(json.dumps(
        {k: v for k, v in bundle.items() if k != "pipeline"}, indent=2))
    print(f"\nSaved models/dormancy_model.pkl  (version {version})")
