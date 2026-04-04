-- =============================================================
-- Colorado Hike Tracker — DuckDB Schema
-- Local-first. Migration-ready (Postgres-compatible DDL).
-- =============================================================

-- ------------------------------------------------------------
-- 1. HIKES  — master trail catalog
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hikes (
    hike_id         VARCHAR PRIMARY KEY,        -- e.g. 'h1', 'grays_peak'
    name            VARCHAR NOT NULL,
    region          VARCHAR,
    latitude        DOUBLE,
    longitude       DOUBLE,

    -- Physical characteristics (priors for Naismith model)
    distance_mi     DOUBLE NOT NULL,            -- round-trip miles
    elevation_gain_ft INTEGER NOT NULL,         -- total gain, ft
    high_point_ft   INTEGER,                    -- summit elevation

    -- Classification
    difficulty      VARCHAR CHECK (difficulty IN ('easy','moderate','hard')),
    is_14er         BOOLEAN DEFAULT FALSE,
    tags            VARCHAR[],                  -- ['photo','iconic','waterfall',...]

    -- Content / notes
    description     VARCHAR,
    permit_required BOOLEAN DEFAULT FALSE,
    best_months     VARCHAR[],                  -- ['Jun','Jul','Aug',...]

    -- Metadata
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ------------------------------------------------------------
-- 2. ACTIVITIES  — raw Strava import, one row per activity
--    Populated by ingest_strava.py
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS activities (
    activity_id         BIGINT PRIMARY KEY,     -- Strava activity ID
    strava_name         VARCHAR,
    activity_type       VARCHAR,                -- 'Hike', 'Run', 'Walk'
    activity_date       DATE NOT NULL,
    start_time          TIME,

    -- Core metrics (source of truth for model training)
    distance_mi         DOUBLE,
    elevation_gain_ft   INTEGER,
    moving_time_sec     INTEGER,                -- Strava moving time (pauses excluded)
    elapsed_time_sec    INTEGER,                -- wall-clock time incl. pauses
    
    -- Derived
    moving_pace_min_mi  DOUBLE GENERATED ALWAYS AS (
                            CASE WHEN distance_mi > 0
                            THEN (moving_time_sec / 60.0) / distance_mi
                            ELSE NULL END
                        ) VIRTUAL,

    -- Optional enrichment
    average_hr          INTEGER,
    max_hr              INTEGER,
    average_cadence     DOUBLE,
    gear_id             VARCHAR,                -- maps to gear table later

    -- Source tracking
    gpx_filename        VARCHAR,
    imported_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ------------------------------------------------------------
-- 3. HIKE_ATTEMPTS  — links an activity to a tracked hike
--    One row per (hike × date). The core comparison table.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hike_attempts (
    attempt_id          INTEGER PRIMARY KEY,    -- autoincrement in app layer
    hike_id             VARCHAR REFERENCES hikes(hike_id),
    activity_id         BIGINT REFERENCES activities(activity_id),
    attempt_date        DATE NOT NULL,

    -- Actual times (minutes)
    actual_moving_min   DOUBLE,                 -- from Strava moving_time
    actual_elapsed_min  DOUBLE,                 -- wall-clock
    summit_pause_min    DOUBLE DEFAULT 0,       -- time paused at top(s); you log this
    -- Corrected moving time = actual_moving_min + summit_pause_min
    -- (because you pause Strava at summit, so moving_time understimates true pace)

    -- Model prediction (logged at prediction time, before the hike)
    predicted_median_min    DOUBLE,
    predicted_lower_90_min  DOUBLE,             -- 5th percentile
    predicted_upper_90_min  DOUBLE,             -- 95th percentile
    prior_used              VARCHAR,            -- 'naismith_only' | 'posterior_vN'

    -- Conditions at time of attempt
    trail_condition     VARCHAR CHECK (trail_condition IN (
                            'dry','muddy','patchy_snow','snow_covered','icy')),
    weather_desc        VARCHAR,                -- free text, e.g. "partly cloudy 55F"
    temperature_f       INTEGER,
    wind_mph            INTEGER,

    -- Personal state
    sleep_quality       INTEGER CHECK (sleep_quality BETWEEN 1 AND 5),
    pack_weight_lb      DOUBLE,
    rpe                 INTEGER CHECK (rpe BETWEEN 1 AND 10),  -- felt difficulty post-hike
    notes               VARCHAR,

    -- Outcome
    completed_summit    BOOLEAN DEFAULT TRUE,
    turned_back_reason  VARCHAR,

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ------------------------------------------------------------
-- 4. MODEL_PARAMS  — persists posterior after each hike
--    Log-normal likelihood: log(pace) ~ Normal(mu, sigma)
--    Prior: Naismith-seeded, then updated via conjugate update
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_params (
    version_id          INTEGER PRIMARY KEY,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    based_on_n_hikes    INTEGER,

    -- Log-normal parameters for moving pace (min/mile)
    -- Modeled as: log(pace_min_mi) ~ Normal(mu_log_pace, sigma_log_pace)
    mu_log_pace         DOUBLE NOT NULL,        -- posterior mean of log(pace)
    sigma_log_pace      DOUBLE NOT NULL,        -- posterior std of log(pace)
    
    -- Elevation penalty (additional min per 100 ft gain)
    -- Naismith baseline = 1.0 min/100ft, updated from data
    elev_penalty_mu     DOUBLE NOT NULL DEFAULT 1.0,
    elev_penalty_sigma  DOUBLE NOT NULL DEFAULT 0.3,

    -- Covariate betas (log-scale adjustments)
    beta_sleep          DOUBLE DEFAULT 0.0,     -- per sleep point (1-5 scale)
    beta_pack_weight    DOUBLE DEFAULT 0.0,     -- per lb over 20lb baseline
    beta_trail_snow     DOUBLE DEFAULT 0.0,     -- snow_covered vs dry
    beta_trail_mud      DOUBLE DEFAULT 0.0,     -- muddy vs dry

    -- Model diagnostics
    loo_cv_score        DOUBLE,                 -- leave-one-out CV
    posterior_predictive_rmse DOUBLE,
    notes               VARCHAR
);

-- ------------------------------------------------------------
-- 5. COVARIATES  — extensible key-value store per attempt
--    Lets you add fields later without schema migration
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS covariates (
    covariate_id    INTEGER PRIMARY KEY,
    attempt_id      INTEGER REFERENCES hike_attempts(attempt_id),
    key             VARCHAR NOT NULL,           -- e.g. 'altitude_sickness_scale'
    value_num       DOUBLE,                     -- numeric value if applicable
    value_text      VARCHAR,                    -- text value if applicable
    unit            VARCHAR,                    -- e.g. 'lbs', 'mph', '1-10'
    recorded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ------------------------------------------------------------
-- 6. STRAVA_SYNC_LOG  — tracks import state
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strava_sync_log (
    sync_id         INTEGER PRIMARY KEY,
    synced_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source_file     VARCHAR,
    activities_imported INTEGER,
    activities_skipped  INTEGER,
    errors          VARCHAR
);

-- ------------------------------------------------------------
-- Views
-- ------------------------------------------------------------

-- Comparison view: predicted vs actual per attempt
CREATE VIEW IF NOT EXISTS v_predictions_vs_actual AS
SELECT
    ha.attempt_id,
    h.name                                          AS hike_name,
    h.difficulty,
    h.distance_mi,
    h.elevation_gain_ft,
    ha.attempt_date,
    ha.predicted_median_min,
    ha.predicted_lower_90_min,
    ha.predicted_upper_90_min,
    ha.actual_moving_min,
    ha.summit_pause_min,
    (ha.actual_moving_min + ha.summit_pause_min)    AS corrected_moving_min,
    -- Prediction error on corrected time
    ((ha.actual_moving_min + ha.summit_pause_min)
        - ha.predicted_median_min)                  AS error_min,
    -- Was actual within the 90% CI?
    CASE WHEN (ha.actual_moving_min + ha.summit_pause_min)
              BETWEEN ha.predicted_lower_90_min
              AND ha.predicted_upper_90_min
         THEN TRUE ELSE FALSE END                   AS within_90ci,
    ha.trail_condition,
    ha.sleep_quality,
    ha.rpe,
    ha.notes
FROM hike_attempts ha
JOIN hikes h ON h.hike_id = ha.hike_id;

-- Personal performance summary per hike
CREATE VIEW IF NOT EXISTS v_hike_summary AS
SELECT
    h.hike_id,
    h.name,
    h.difficulty,
    h.distance_mi,
    h.elevation_gain_ft,
    COUNT(ha.attempt_id)                            AS n_attempts,
    MIN(ha.actual_moving_min + ha.summit_pause_min) AS best_corrected_min,
    AVG(ha.actual_moving_min + ha.summit_pause_min) AS avg_corrected_min,
    AVG(ha.rpe)                                     AS avg_rpe,
    MAX(ha.attempt_date)                            AS last_attempted
FROM hikes h
LEFT JOIN hike_attempts ha ON ha.hike_id = h.hike_id
GROUP BY h.hike_id, h.name, h.difficulty, h.distance_mi, h.elevation_gain_ft;
