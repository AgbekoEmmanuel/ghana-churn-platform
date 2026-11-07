"""
Phase 3 - train_model.py
========================
Trains the churn model on the historical (Kaggle) customers in Neon and
saves it to models/churn_model.pkl for the scoring script and the web API.

Steps:
  1. Pull the labelled rows (data_source = 'kaggle') from Neon.
  2. Split them into train / validation / test sets (60 / 20 / 20).
  3. Train 3 models, each set up to handle class imbalance:
       - Logistic Regression  (simple baseline)
       - Random Forest
       - XGBoost
  4. Pick the best one on the validation set.
  5. Report honest final scores on the untouched test set.
  6. Check how much the SYNTHETIC columns (Ghana channels + 360 activity)
     contribute (ablation test).
  7. CALIBRATE the probabilities so 0.70 really means "about 70% leave".
  8. Save the model + its settings to models/churn_model.pkl.

Run (venv active):
    python scripts\\train_model.py
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import joblib                                   # saves/loads Python objects (our .pkl)
import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, brier_score_loss, confusion_matrix,
                             f1_score, precision_score, recall_score,
                             roc_auc_score)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

try:
    from xgboost import XGBClassifier
except ImportError:                             # the script still works without XGBoost
    XGBClassifier = None
    print("Note: xgboost is not installed, so it will be skipped. (pip install xgboost)")

# ---------------------------------------------------------------------
# 0. Settings
# ---------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
MODEL_DIR = PROJECT_DIR / "models"
MODEL_DIR.mkdir(exist_ok=True)

load_dotenv(PROJECT_DIR / ".env")
DATABASE_URL = os.getenv("DATABASE_URL")
SEED = 42

# Which columns the model uses. customer_id is NOT a feature:
# it's just a label, and would let the model memorise people.
NUMERIC = ["credit_score", "age", "tenure", "balance", "products_number",
           "credit_card", "active_member", "estimated_salary"]
# SYNTHETIC columns: Ghana digital channels + the 360 activity view.
GHANA_NUMERIC = ["uses_mobile_app", "uses_ussd", "uses_internet_banking",
                 "momo_linked", "failed_txn_count_90d",
                 "days_since_last_txn", "branch_visits_90d", "atm_withdrawals_90d",
                 "total_txn_count_90d", "salary_credited", "complaints_90d",
                 "balance_change_3m_pct"]
# Gender is deliberately NOT used: deciding who gets retention offers based
# on gender raises fairness and regulatory concerns, and it isn't needed.
CATEGORICAL = ["country"]
GHANA_CATEGORICAL = ["region", "primary_channel"]
TARGET = "churn"


# ---------------------------------------------------------------------
# 1. Load the labelled data from Neon
# ---------------------------------------------------------------------
def load_training_data() -> pd.DataFrame:
    cols = NUMERIC + GHANA_NUMERIC + CATEGORICAL + GHANA_CATEGORICAL + [TARGET]
    sql = f"""
        SELECT {", ".join(cols)}
        FROM bank_customers
        WHERE data_source = 'kaggle' AND churn IS NOT NULL
    """
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            df = pd.DataFrame(cur.fetchall(), columns=cols)
    finally:
        conn.close()

    # Postgres NUMERIC comes back as Python Decimal; convert to normal floats.
    for c in NUMERIC + GHANA_NUMERIC + [TARGET]:
        df[c] = pd.to_numeric(df[c])
    print(f"Loaded {len(df):,} labelled customers. Churn rate: {df[TARGET].mean():.1%}")
    return df


# ---------------------------------------------------------------------
# 2. Build a model "pipeline" = preprocessing + model in ONE object.
#    Saving the whole pipeline means the scoring script can feed in raw
#    rows from the database and get predictions, with no extra steps.
# ---------------------------------------------------------------------
def make_pipeline(model, numeric, categorical, scale=False):
    num_step = StandardScaler() if scale else "passthrough"   # trees don't need scaling
    pre = ColumnTransformer([
        ("num", num_step, numeric),
        # One-hot: turns 'country' into country_France / country_Spain / ...
        # handle_unknown='ignore' stops crashes if a new category appears later.
        ("cat", OneHotEncoder(handle_unknown="ignore"), categorical),
    ])
    return Pipeline([("prep", pre), ("model", model)])


def candidate_models(pos_weight):
    """
    CLASS IMBALANCE: only ~20% of customers churn. Left alone, models get lazy
    and predict 'stays' for almost everyone. Each model below is told to treat
    a churner as more important:
      - class_weight='balanced'  -> sklearn weights churners ~4x
      - scale_pos_weight         -> XGBoost's version of the same idea
    """
    models = {
        "Logistic Regression": (LogisticRegression(class_weight="balanced", max_iter=2000), True),
        "Random Forest": (RandomForestClassifier(
            n_estimators=400, min_samples_leaf=5, class_weight="balanced_subsample",
            n_jobs=-1, random_state=SEED), False),
    }
    if XGBClassifier is not None:
        models["XGBoost"] = (XGBClassifier(
            n_estimators=400, max_depth=4, learning_rate=0.05, subsample=0.8,
            colsample_bytree=0.8, scale_pos_weight=pos_weight,
            eval_metric="logloss", random_state=SEED, n_jobs=-1), False)
    return models


def best_threshold(y_true, proba):
    """
    A model outputs a probability (e.g. 0.37). The THRESHOLD decides when we
    call it 'will churn'. 0.5 is not always best on imbalanced data, so we try
    many values and keep the one with the best F1 (balance of precision & recall).
    """
    grid = np.arange(0.10, 0.91, 0.01)
    scores = [f1_score(y_true, proba >= t) for t in grid]
    return round(float(grid[int(np.argmax(scores))]), 2)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
if __name__ == "__main__":
    df = load_training_data()
    X, y = df.drop(columns=[TARGET]), df[TARGET]

    # 60% train / 20% validation / 20% test.
    # stratify=y keeps the ~20% churn rate the same in every split.
    X_train, X_tmp, y_train, y_tmp = train_test_split(X, y, test_size=0.4, stratify=y, random_state=SEED)
    X_val, X_test, y_val, y_test = train_test_split(X_tmp, y_tmp, test_size=0.5, stratify=y_tmp, random_state=SEED)
    print(f"Train {len(X_train):,} | Validation {len(X_val):,} | Test {len(X_test):,}\n")

    all_num = NUMERIC + GHANA_NUMERIC
    all_cat = CATEGORICAL + GHANA_CATEGORICAL
    pos_weight = (y_train == 0).sum() / (y_train == 1).sum()

    # ---- 3. Train and compare on the VALIDATION set ----
    print("Model comparison on the validation set:")
    print(f"{'Model':<22}{'ROC-AUC':>9}{'PR-AUC':>9}")
    results = {}
    for name, (model, scale) in candidate_models(pos_weight).items():
        pipe = make_pipeline(model, all_num, all_cat, scale)
        pipe.fit(X_train, y_train)
        p = pipe.predict_proba(X_val)[:, 1]
        results[name] = (pipe, average_precision_score(y_val, p), roc_auc_score(y_val, p))
        print(f"{name:<22}{results[name][2]:>9.3f}{results[name][1]:>9.3f}")

    # PR-AUC focuses on how well we catch churners, so it's the fairer
    # yardstick when churners are the minority. We pick the best by PR-AUC.
    best_name = max(results, key=lambda k: results[k][1])
    best_pipe = results[best_name][0]
    print(f"\nWinner: {best_name}")

    # ---- CALIBRATION ----
    # We told the model churners count ~4x (class weighting). That fixes the
    # imbalance but INFLATES the probabilities (lots of 0.99s). Calibration
    # learns a small correction curve on the validation set so that, e.g.,
    # customers scored 0.30 really do leave about 30% of the time.
    try:
        from sklearn.frozen import FrozenEstimator        # scikit-learn 1.6+
        calibrated = CalibratedClassifierCV(FrozenEstimator(best_pipe), method="sigmoid")
    except ImportError:                                    # older scikit-learn
        calibrated = CalibratedClassifierCV(best_pipe, method="sigmoid", cv="prefit")
    calibrated.fit(X_val, y_val)

    raw_test = best_pipe.predict_proba(X_test)[:, 1]
    p_test = calibrated.predict_proba(X_test)[:, 1]
    print(f"Calibration: Brier score {brier_score_loss(y_test, raw_test):.3f} -> "
          f"{brier_score_loss(y_test, p_test):.3f}  (lower = more honest probabilities)")
    print(f"  Average predicted risk {raw_test.mean():.1%} -> {p_test.mean():.1%} "
          f"(actual churn rate {y_test.mean():.1%})")

    # Threshold is tuned on the CALIBRATED validation probabilities.
    threshold = best_threshold(y_val, calibrated.predict_proba(X_val)[:, 1])
    print(f"Decision threshold tuned to {threshold:.2f}")

    # ---- 5. Final, honest score on the TEST set (never seen before) ----
    pred = (p_test >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_test, pred).ravel()
    metrics = {
        "roc_auc": round(roc_auc_score(y_test, p_test), 4),
        "pr_auc": round(average_precision_score(y_test, p_test), 4),
        "precision": round(precision_score(y_test, pred), 4),
        "recall": round(recall_score(y_test, pred), 4),
        "f1": round(f1_score(y_test, pred), 4),
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }
    print("\n=== Test set results ===")
    print(f"ROC-AUC   {metrics['roc_auc']:.3f}   (0.5 = coin flip, 1.0 = perfect)")
    print(f"PR-AUC    {metrics['pr_auc']:.3f}   (a random guess scores about the churn rate, {y_test.mean():.2f})")
    print(f"Precision {metrics['precision']:.3f}   (of customers we flag, share who really left)")
    print(f"Recall    {metrics['recall']:.3f}   (of customers who left, share we caught)")
    print(f"Confusion matrix: caught {tp} churners, missed {fn}, false alarms {fp}, correct 'stays' {tn}")

    print("\nDo the probabilities mean what they say? (test set, grouped by predicted risk)")
    bins = pd.cut(p_test, [0, 0.1, 0.3, 0.5, 0.7, 1.0], include_lowest=True)
    table = pd.DataFrame({"predicted": p_test, "actual": y_test.to_numpy()}).groupby(bins, observed=True)
    print(table.agg(customers=("actual", "size"), avg_predicted=("predicted", "mean"),
                    actually_left=("actual", "mean")).round(2).to_string())

    # ---- 6. Ablation: how much do the SYNTHETIC Ghana columns add? ----
    model, scale = candidate_models(pos_weight)[best_name]
    base_pipe = make_pipeline(model, NUMERIC, CATEGORICAL, scale).fit(X_train, y_train)
    auc_without = roc_auc_score(y_test, base_pipe.predict_proba(X_test)[:, 1])
    metrics["roc_auc_without_ghana_features"] = round(auc_without, 4)
    print(f"\nAblation: ROC-AUC with only the real Kaggle columns = {auc_without:.3f}  "
          f"(with = {metrics['roc_auc']:.3f})")
    print("  Any gain comes from patterns WE built into the synthetic data, not real Ghana evidence.")

    # ---- Feature importance (tree models only) ----
    inner = best_pipe.named_steps["model"]
    if hasattr(inner, "feature_importances_"):
        names = best_pipe.named_steps["prep"].get_feature_names_out()
        imp = pd.Series(inner.feature_importances_, index=[n.split("__", 1)[1] for n in names])
        print("\nTop 10 features the model relies on:")
        print(imp.sort_values(ascending=False).head(10).round(3).to_string())

    # ---- 7. Save ----
    version = "v2-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bundle = {
        "pipeline": calibrated,                 # preprocessing + model + calibration
        "threshold": threshold,                 # cut-off for "will churn"
        "features": all_num + all_cat,          # columns the pipeline expects
        "model_name": best_name + " (calibrated)",
        "model_version": version,
        "metrics": metrics,
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    joblib.dump(bundle, MODEL_DIR / "churn_model.pkl")
    (MODEL_DIR / "model_metrics.json").write_text(json.dumps(
        {k: v for k, v in bundle.items() if k != "pipeline"}, indent=2))
    print(f"\nSaved models/churn_model.pkl  (version {version})")
    print("Phase 3 complete.")
