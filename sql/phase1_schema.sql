-- =====================================================================
-- PHASE 1: bank_customers table
-- Ghana Bank Churn Prediction Platform
-- Paste this whole file into the Neon "SQL Editor" and click Run.
-- It is safe to run twice (IF NOT EXISTS).
-- =====================================================================

CREATE TABLE IF NOT EXISTS bank_customers (

    -- ---------- Row identity ----------
    id                     BIGSERIAL PRIMARY KEY,          -- auto number the database manages
    customer_id            BIGINT UNIQUE NOT NULL,          -- the bank's customer number (from Kaggle, or made up by the simulator)

    -- ---------- Original Kaggle columns (REAL data) ----------
    credit_score           INTEGER CHECK (credit_score BETWEEN 300 AND 900),
    country                VARCHAR(50),                     -- Kaggle has France / Spain / Germany
    gender                 VARCHAR(10),
    age                    INTEGER CHECK (age BETWEEN 18 AND 100),
    tenure                 INTEGER CHECK (tenure >= 0),     -- years with the bank
    balance                NUMERIC(14,2) CHECK (balance >= 0),
    products_number        INTEGER CHECK (products_number BETWEEN 1 AND 10),
    credit_card            SMALLINT CHECK (credit_card IN (0,1)),
    active_member          SMALLINT CHECK (active_member IN (0,1)),
    estimated_salary       NUMERIC(14,2) CHECK (estimated_salary >= 0),

    -- ---------- Ghana digital channel columns (SYNTHETIC data) ----------
    -- Kaggle does not have these. In Phase 2 we generate them in Python,
    -- linked to the real columns so they behave plausibly.
    region                 VARCHAR(30),                     -- e.g. Greater Accra, Ashanti, Northern
    uses_mobile_app        SMALLINT CHECK (uses_mobile_app IN (0,1)),
    uses_ussd              SMALLINT CHECK (uses_ussd IN (0,1)),
    uses_internet_banking  SMALLINT CHECK (uses_internet_banking IN (0,1)),
    momo_linked            SMALLINT CHECK (momo_linked IN (0,1)),  -- account linked to a Mobile Money wallet
    primary_channel        VARCHAR(20) CHECK (primary_channel IN ('branch','ussd','mobile_app','internet_banking','momo')),
    failed_txn_count_90d   INTEGER CHECK (failed_txn_count_90d >= 0), -- failed transactions in the last 90 days

    -- ---------- Target ----------
    churn                  SMALLINT CHECK (churn IN (0,1)), -- 1 = left the bank. NULL for live customers (we don't know yet)

    -- ---------- Honesty / lineage labels ----------
    data_source            VARCHAR(20) NOT NULL DEFAULT 'simulator'
                           CHECK (data_source IN ('kaggle','simulator')),
    ghana_features_synthetic BOOLEAN NOT NULL DEFAULT TRUE, -- TRUE = the Ghana channel columns were generated, not observed
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- ---------- Filled later by the scoring script (Phase 5) ----------
    churn_probability      NUMERIC(5,4) CHECK (churn_probability BETWEEN 0 AND 1),
    risk_band              VARCHAR(10) CHECK (risk_band IN ('low','medium','high')),
    model_version          VARCHAR(30),
    scored_at              TIMESTAMPTZ                      -- NULL means "not scored yet"
);

-- Makes "find me rows that are not scored yet" fast (used by Phase 5).
CREATE INDEX IF NOT EXISTS idx_bank_customers_unscored
    ON bank_customers (created_at)
    WHERE scored_at IS NULL;

-- Makes "show me the newest customers" fast (used by the web platform).
CREATE INDEX IF NOT EXISTS idx_bank_customers_created_at
    ON bank_customers (created_at DESC);

-- ---------- Check it worked ----------
-- You should see 27 rows listing every column and its type.
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'bank_customers'
ORDER BY ordinal_position;
