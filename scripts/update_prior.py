import duckdb, math, statistics

con = duckdb.connect(r'data\hike_tracker.db')

# -- Step 0: Drop dependent views, alter table, recreate views -------------
con.execute("DROP VIEW IF EXISTS v_predictions_vs_actual")
con.execute("DROP VIEW IF EXISTS v_hike_summary")

try:
    con.execute("ALTER TABLE activities ADD COLUMN exclude_from_model BOOLEAN DEFAULT FALSE")
    print("Added exclude_from_model column")
except Exception as e:
    print(f"Column note: {e}")

try:
    con.execute("ALTER TABLE activities ADD COLUMN exclude_reason VARCHAR")
    print("Added exclude_reason column")
except Exception as e:
    print(f"Column note: {e}")

# Recreate views
con.execute("""
    CREATE VIEW v_predictions_vs_actual AS
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
        ((ha.actual_moving_min + ha.summit_pause_min)
            - ha.predicted_median_min)                  AS error_min,
        CASE WHEN (ha.actual_moving_min + ha.summit_pause_min)
                  BETWEEN ha.predicted_lower_90_min
                  AND ha.predicted_upper_90_min
             THEN TRUE ELSE FALSE END                   AS within_90ci,
        ha.trail_condition,
        ha.sleep_quality,
        ha.rpe,
        ha.notes
    FROM hike_attempts ha
    JOIN hikes h ON h.hike_id = ha.hike_id
""")

con.execute("""
    CREATE VIEW v_hike_summary AS
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
    GROUP BY h.hike_id, h.name, h.difficulty, h.distance_mi, h.elevation_gain_ft
""")
print("Views recreated")

# -- Step 1: Flag descents -------------------------------------------------
con.execute("""
    UPDATE activities
    SET exclude_from_model = TRUE,
        exclude_reason = 'likely_descent'
    WHERE activity_type = 'Hike'
      AND distance_mi > 4
      AND elevation_gain_ft / distance_mi < 100
      AND (moving_time_sec/60.0) / distance_mi > 20
""")

flagged = con.execute("""
    SELECT strava_name, distance_mi, elevation_gain_ft,
           ROUND((moving_time_sec/60.0)/distance_mi,1) as pace,
           exclude_reason
    FROM activities
    WHERE exclude_from_model = TRUE
""").fetchall()
print(f"\nFlagged {len(flagged)} activities as excluded:")
for r in flagged:
    print(f"  {str(r[0]):<45} {r[1]:.1f}mi +{r[2]}ft {r[3]} min/mi [{r[4]}]")

# -- Step 2: Conjugate update from clean training set ----------------------
clean = con.execute("""
    SELECT strava_name, distance_mi, elevation_gain_ft,
           moving_time_sec/60.0 as moving_min,
           (moving_time_sec/60.0) / distance_mi as pace_min_mi
    FROM activities
    WHERE activity_type = 'Hike'
      AND exclude_from_model = FALSE
      AND distance_mi > 2
      AND elevation_gain_ft > 300
      AND moving_time_sec > 0
""").fetchall()

paces     = [r[4] for r in clean]
log_paces = [math.log(p) for p in paces]
n         = len(paces)

mu_data  = statistics.mean(log_paces)
sd_data  = statistics.stdev(log_paces) if n > 1 else 0.25

mu_0, tau_0, sigma = math.log(20.0), 0.25, sd_data
prior_prec = 1.0 / tau_0**2
likel_prec = n / sigma**2
post_prec  = prior_prec + likel_prec
post_var   = 1.0 / post_prec
post_mu    = post_var * (prior_prec * mu_0 + likel_prec * mu_data)
post_sigma = math.sqrt(post_var)

print(f"\n-- Posterior update ----------------------------------")
print(f"  Training hikes (clean):  {n}")
print(f"  Observed log-pace mean:  {mu_data:.3f}  => {math.exp(mu_data):.1f} min/mi")
print(f"  Observed log-pace stdev: {sd_data:.3f}")
print(f"  Prior:     mu={mu_0:.3f} ({math.exp(mu_0):.1f} min/mi)  tau={tau_0:.3f}")
print(f"  Posterior: mu={post_mu:.3f} ({math.exp(post_mu):.1f} min/mi)  tau={post_sigma:.3f}")
print(f"  Shrinkage toward prior:  {(mu_data - post_mu):.3f} log units")

# -- Step 3: Write to model_params -----------------------------------------
next_v = con.execute("SELECT COALESCE(MAX(version_id),0)+1 FROM model_params").fetchone()[0]
con.execute("""
    INSERT INTO model_params (
        version_id, based_on_n_hikes,
        mu_log_pace, sigma_log_pace,
        elev_penalty_mu, elev_penalty_sigma,
        beta_sleep, beta_pack_weight, beta_trail_snow, beta_trail_mud,
        notes
    ) VALUES (?,?,?,?,1.0,0.30,0.0,0.0,0.10,0.05,?)
""", [
    next_v, n, post_mu, sd_data,
    f"Seeded from {n} real Strava hikes. Observed mean={math.exp(mu_data):.1f} min/mi. Conjugate update from Naismith prior."
])
print(f"  Written as model_params v{next_v}")

# -- Step 4: Show prediction impact ----------------------------------------
print(f"\n-- Prediction change on sample hikes ----------------")
samples = [
    ("Grays Peak",            8.0,  3000),
    ("Longs Peak (Keyhole)", 14.5,  5100),
    ("Mt Sanitas",            3.1,  1343),
    ("Quandary Peak",         6.75, 3350),
]
print(f"  {'Hike':<28} {'Old (Naismith)':>16} {'New (your data)':>16} {'Diff':>6}")
print(f"  {'-'*70}")
for name, dist, elev in samples:
    old = math.exp(mu_0) * dist + 1.0 * (elev/100)
    new = math.exp(post_mu) * dist + 1.0 * (elev/100)
    print(f"  {name:<28} {old:>6.0f}min ({old/60:.1f}h)  {new:>6.0f}min ({new/60:.1f}h)  {new-old:>+5.0f}m")

con.close()
