# Colorado Hike Tracker — Data Collection Framework

## Project structure

```
hike_tracker/
├── data/
│   └── hike_tracker.db          ← DuckDB database (git-ignored, back up manually)
├── models/
│   └── model.py                 ← Bayesian prediction engine
├── scripts/
│   ├── collect.py               ← CLI: init / predict / log / status / compare
│   └── ingest_strava.py         ← Strava CSV importer
├── exports/
│   └── prediction_h11.json      ← Generated prediction files for the viz app
└── schema.sql                   ← DDL — source of truth for all tables
```

---

## Setup

```bash
pip install duckdb numpy scipy
cd hike_tracker/scripts
python collect.py init --db ../data/hike_tracker.db
```

---

## Strava export → import

1. Strava → Settings → My Account → Download or Delete Your Account → Request Your Archive
2. You'll get a zip with `activities.csv` + per-activity GPX files
3. Import:

```bash
# Dry run first to verify parsing
python ingest_strava.py --csv ~/Downloads/export/activities.csv --dry-run

# Full import
python ingest_strava.py --csv ~/Downloads/export/activities.csv --db ../data/hike_tracker.db
```

The importer only pulls Hike/Walk/Run/Trail Run activity types and converts
Strava's meters → miles/feet. Duplicate activity IDs are silently skipped so
you can re-run safely.

---

## Workflow: before a hike

```bash
# Get a prediction with current conditions
python collect.py predict h11 --sleep 4 --pack 22 --condition dry --db ../data/hike_tracker.db
```

Output:
```
Grays Peak
  Predicted moving time: 3h 24m (90% CI: 2h 48m–4h 12m)
  Flat component: 2h 42m  Elevation: +42m  Covariate adj: -3.2min
  Model: naismith_only (trained on 0 hikes)
```

This also writes `exports/prediction_h11.json` for the viz app.

---

## Workflow: after a hike

```bash
python collect.py log --db ../data/hike_tracker.db
```

The interactive prompt asks for:
- Strava activity ID (from the URL: strava.com/activities/12345678)
- Actual moving time (minutes, directly from Strava)
- **Summit pause time** — minutes you paused at top(s); this is the correction
  factor. If Strava shows 3h 20m moving but you paused 25 min at summit,
  enter 25. Corrected = 3h 45m.
- Trail conditions, weather, temperature
- Sleep quality (1–5), pack weight, RPE
- Notes (free text — anything you'd want for content captions)

After logging, the posterior updates automatically.

---

## The Bayesian model

### Why log-normal?

Hiking pace (min/mile) is right-skewed — easy days cluster tightly, but hard
days (altitude sickness, bad conditions, wrong turn) have long right tails.
Modeling `log(pace)` as normal handles this correctly and allows conjugate
Bayesian updating.

### Prior (Naismith's Rule)

Naismith (1892) heuristic:
- Flat pace: 3 mph = **20 min/mile**
- Elevation: **+1 min per 100 ft gain**

These seed the priors:
```
mu_log_pace  ~ Normal(log(20), 0.25)     → 20 min/mi ± ~5 min/mi at 1 SD
elev_penalty ~ Normal(1.0, 0.30)         → 1 min/100ft ± 0.30
```

### Update rule

After each hike we observe `log(corrected_pace)` and apply a
**conjugate normal-normal update**:

```
Prior:     mu ~ Normal(mu_0, tau_0²)
Likelihood: x_i ~ Normal(mu, sigma²)
Posterior: mu | data ~ Normal(mu_n, tau_n²)

Where:
  1/tau_n² = 1/tau_0² + n/sigma²
  mu_n     = tau_n² * (mu_0/tau_0² + n*x_bar/sigma²)
```

This is analytically exact, fast, and interpretable. Once you have ~15+
hikes it can be upgraded to a full MCMC model (PyMC) to also estimate
covariate betas from data rather than using fixed priors.

### Summit pause correction

Because you pause Strava at summits, `moving_time` from Strava *understates*
your true effort pace — it misses the time moving toward and away from the
paused point. The correction:

```
corrected_moving_min = strava_moving_min + summit_pause_min
```

The model trains on `corrected_moving_min` so predictions are calibrated
to your actual experience including the summit push.

### Covariate adjustments (log-pace scale)

| Covariate         | Direction | Default beta | Notes                        |
|-------------------|-----------|-------------|------------------------------|
| sleep_quality     | negative  | 0.0         | better sleep = faster        |
| pack_weight_lb    | positive  | 0.0         | per lb over 20 lb baseline   |
| snow_covered      | positive  | +0.10       | ~10% slower vs dry           |
| muddy/icy         | positive  | +0.05       | ~5% slower vs dry            |

Betas are currently fixed at priors — after ~20 hikes they'll be
estimable from data.

### Model diagnostics

After each update the model logs:
- `loo_cv_score` — leave-one-out CV RMSE (lower = better fit, 1-sigma threshold ≈ 8–12 min)
- `posterior_predictive_rmse` — in-sample fit
- `within_90ci` — flag per attempt: was actual inside the 90% credible interval?

Target: ~90% of attempts should be within the 90% CI. If coverage is lower,
sigma_log_pace needs to increase (wider uncertainty). If higher, the model
is overconfident in a good way — tighten later.

---

## Data dictionary

### `hike_attempts` key fields

| Column              | Type    | Notes                                          |
|---------------------|---------|------------------------------------------------|
| actual_moving_min   | DOUBLE  | Strava moving time, unmodified                 |
| summit_pause_min    | DOUBLE  | Time paused at summit(s) — you enter this      |
| corrected_moving_min| (view)  | actual + pause — what the model trains on      |
| predicted_median_min| DOUBLE  | Model prediction logged *before* the hike      |
| rpe                 | INT 1–10| Rate of Perceived Exertion, logged after        |
| sleep_quality       | INT 1–5 | 5=great sleep, 1=terrible                      |

### `covariates` table

For custom fields that don't fit the main schema:

```python
con.execute("""
    INSERT INTO covariates (attempt_id, key, value_num, unit)
    VALUES (?, 'altitude_sickness_scale', ?, '1-10')
""", [attempt_id, 2])
```

---

## Migration to Supabase (when ready)

```bash
# Export schema + data
duckdb hike_tracker.db -c ".mode csv" -c ".output hike_attempts.csv" \
  "SELECT * FROM hike_attempts"

# Supabase DDL is the same schema.sql — DuckDB syntax is Postgres-compatible
# with minor exceptions (GENERATED ALWAYS AS VIRTUAL → use a regular view)
```

---

## Portfolio notes

This system demonstrates:
- **Bayesian inference** applied to a personal dataset (biostatistics background)
- **Conjugate updating** — analytically exact, interpretable, upgradeable to MCMC
- **Experimental design** — the summit pause correction is a real data quality
  decision with a documented rationale, not just sloppy logging
- **Local-first schema design** with a clear migration path
- **Separation of concerns** — schema / ingestion / model / CLI are cleanly separate
