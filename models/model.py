"""
model.py
--------
Bayesian moving-time predictor for hikes.

Statistical model
-----------------
We model log(moving_pace_min_mi) as Normal(mu, sigma) — i.e. pace is log-normal,
which handles the right skew of hiking data (slow days pull the tail hard).

Moving time prediction:
    T_moving = pace_min_mi * distance_mi + elev_penalty_min_per_100ft * (gain_ft / 100)

Priors (Naismith-seeded)
    Naismith's Rule: 3 mph on flat = 20 min/mi flat pace
    Elevation: +1 min per 100 ft gain
    
    mu_log_pace    ~ Normal(log(20), 0.25)   → prior centered at 20 min/mi, ±~5 min/mi 1SD
    sigma_log_pace ~ HalfNormal(0.20)        → within-person variability
    elev_penalty   ~ Normal(1.0, 0.3)        → min per 100ft, Naismith = 1.0

Update rule
    After each hike attempt we do a Bayesian update:
    - Likelihood: observed log(pace) ~ Normal(mu_log_pace, sigma_log_pace)
    - Conjugate update on mu given sigma (normal-normal, known variance approximation)
    - This is intentionally simple — upgradeable to MCMC (PyMC) once N > ~10

Covariate adjustments (additive on log-pace scale)
    sleep_quality (1-5):  negative beta → better sleep = faster
    pack_weight_lb:       positive beta → heavier = slower  
    trail_condition:      dummy-coded vs 'dry' baseline
"""

import json
import math
from dataclasses import dataclass, asdict
from typing import Optional
import duckdb
import numpy as np
from scipy import stats


# ── Naismith baseline priors ──────────────────────────────────────────────────
NAISMITH_PACE_MIN_MI     = 20.0   # 3 mph flat pace
NAISMITH_ELEV_MIN_100FT  = 1.0    # 1 min per 100 ft gain

PRIOR_MU_LOG_PACE        = math.log(NAISMITH_PACE_MIN_MI)
PRIOR_SIGMA_LOG_PACE     = 0.25   # ~±5 min/mi at 1 SD on raw scale
PRIOR_ELEV_MU            = NAISMITH_ELEV_MIN_100FT
PRIOR_ELEV_SIGMA         = 0.30

# Covariate defaults (no adjustment until data supports it)
PRIOR_BETA_SLEEP         = 0.0
PRIOR_BETA_PACK          = 0.0
PRIOR_BETA_SNOW          = 0.10   # snow → ~10% slower on log scale
PRIOR_BETA_MUD           = 0.05


@dataclass
class ModelParams:
    version_id:             int
    based_on_n_hikes:       int
    mu_log_pace:            float
    sigma_log_pace:         float
    elev_penalty_mu:        float
    elev_penalty_sigma:     float
    beta_sleep:             float
    beta_pack_weight:       float
    beta_trail_snow:        float
    beta_trail_mud:         float
    loo_cv_score:           Optional[float] = None
    posterior_predictive_rmse: Optional[float] = None
    notes:                  Optional[str] = None


@dataclass
class Prediction:
    """Output of predict() — everything needed for display + DB logging."""
    hike_name:          str
    distance_mi:        float
    elevation_gain_ft:  int

    # Point estimates (minutes)
    median_min:         float
    mean_min:           float

    # 90% credible interval
    lower_90_min:       float
    upper_90_min:       float

    # 80% CI (tighter band for display)
    lower_80_min:       float
    upper_80_min:       float

    # Decomposition
    flat_component_min: float
    elev_component_min: float
    covariate_adj_min:  float

    # Model metadata
    prior_used:         str
    n_hikes_trained_on: int

    # Full posterior predictive samples (for distribution plot)
    samples:            list[float]

    def to_hhmm(self, minutes: float) -> str:
        h = int(minutes // 60)
        m = int(minutes % 60)
        return f"{h}h {m:02d}m" if h > 0 else f"{m}m"

    def summary(self) -> str:
        return (
            f"{self.hike_name}\n"
            f"  Predicted moving time: {self.to_hhmm(self.median_min)} "
            f"(90% CI: {self.to_hhmm(self.lower_90_min)}–{self.to_hhmm(self.upper_90_min)})\n"
            f"  Flat component: {self.to_hhmm(self.flat_component_min)}  "
            f"Elevation: +{self.to_hhmm(self.elev_component_min)}  "
            f"Covariate adj: {self.covariate_adj_min:+.1f}min\n"
            f"  Model: {self.prior_used} (trained on {self.n_hikes_trained_on} hikes)"
        )


class HikePaceModel:
    """
    Loads current posterior from DB, generates predictions, persists updates.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.params = self._load_params()

    def _load_params(self) -> ModelParams:
        """Load most recent posterior, falling back to Naismith priors."""
        con = duckdb.connect(self.db_path, read_only=True)
        row = con.execute("""
            SELECT version_id, based_on_n_hikes,
                   mu_log_pace, sigma_log_pace,
                   elev_penalty_mu, elev_penalty_sigma,
                   beta_sleep, beta_pack_weight, beta_trail_snow, beta_trail_mud,
                   loo_cv_score, posterior_predictive_rmse, notes
            FROM model_params
            ORDER BY version_id DESC
            LIMIT 1
        """).fetchone()
        con.close()

        if row:
            return ModelParams(*row)

        # No data yet — use Naismith priors
        return ModelParams(
            version_id=0,
            based_on_n_hikes=0,
            mu_log_pace=PRIOR_MU_LOG_PACE,
            sigma_log_pace=PRIOR_SIGMA_LOG_PACE,
            elev_penalty_mu=PRIOR_ELEV_MU,
            elev_penalty_sigma=PRIOR_ELEV_SIGMA,
            beta_sleep=PRIOR_BETA_SLEEP,
            beta_pack_weight=PRIOR_BETA_PACK,
            beta_trail_snow=PRIOR_BETA_SNOW,
            beta_trail_mud=PRIOR_BETA_MUD,
            notes="Naismith prior, no personal data yet"
        )

    def predict(
        self,
        hike_name:          str,
        distance_mi:        float,
        elevation_gain_ft:  int,
        sleep_quality:      Optional[int]   = None,   # 1–5
        pack_weight_lb:     Optional[float] = None,   # lbs
        trail_condition:    Optional[str]   = None,   # 'dry','muddy','snow_covered',...
        n_samples:          int             = 10_000,
    ) -> Prediction:
        p = self.params

        # ── Covariate adjustment (log-pace scale) ──────────────────────────
        cov_adj_log = 0.0
        if sleep_quality is not None:
            # sleep=5 (great) → negative adjustment (faster); sleep=1 → slower
            cov_adj_log += p.beta_sleep * (3 - sleep_quality)  # centered at 3
        if pack_weight_lb is not None:
            cov_adj_log += p.beta_pack_weight * max(0, pack_weight_lb - 20)
        if trail_condition == 'snow_covered':
            cov_adj_log += p.beta_trail_snow
        elif trail_condition in ('muddy', 'icy'):
            cov_adj_log += p.beta_trail_mud

        # ── Posterior predictive samples ───────────────────────────────────
        # Sample pace from posterior
        log_pace_samples = np.random.normal(
            loc=p.mu_log_pace + cov_adj_log,
            scale=p.sigma_log_pace,
            size=n_samples
        )
        pace_samples = np.exp(log_pace_samples)  # min/mi

        # Sample elevation penalty
        elev_penalty_samples = np.random.normal(
            loc=p.elev_penalty_mu,
            scale=p.elev_penalty_sigma,
            size=n_samples
        )
        elev_penalty_samples = np.clip(elev_penalty_samples, 0.3, 3.0)

        # Total time samples
        flat_time_samples = pace_samples * distance_mi
        elev_time_samples = elev_penalty_samples * (elevation_gain_ft / 100.0)
        total_samples = flat_time_samples + elev_time_samples

        # ── Point estimates ────────────────────────────────────────────────
        prior_label = ("naismith_only" if p.based_on_n_hikes == 0
                       else f"posterior_v{p.version_id}")

        covariate_adj_min = (math.exp(p.mu_log_pace + cov_adj_log) - math.exp(p.mu_log_pace)) * distance_mi

        return Prediction(
            hike_name           = hike_name,
            distance_mi         = distance_mi,
            elevation_gain_ft   = elevation_gain_ft,
            median_min          = float(np.median(total_samples)),
            mean_min            = float(np.mean(total_samples)),
            lower_90_min        = float(np.percentile(total_samples, 5)),
            upper_90_min        = float(np.percentile(total_samples, 95)),
            lower_80_min        = float(np.percentile(total_samples, 10)),
            upper_80_min        = float(np.percentile(total_samples, 90)),
            flat_component_min  = float(np.median(flat_time_samples)),
            elev_component_min  = float(np.median(elev_time_samples)),
            covariate_adj_min   = round(covariate_adj_min, 1),
            prior_used          = prior_label,
            n_hikes_trained_on  = p.based_on_n_hikes,
            samples             = total_samples.tolist(),
        )

    def update_posterior(self, db_path: Optional[str] = None) -> ModelParams:
        """
        Re-estimate posterior from all completed hike_attempts.
        Uses conjugate normal-normal update on log(pace).

        Called automatically after logging a new attempt.
        Returns new ModelParams (also persisted to DB).
        """
        db = db_path or self.db_path
        con = duckdb.connect(db)

        rows = con.execute("""
            SELECT
                a.distance_mi,
                a.elevation_gain_ft,
                (ha.actual_moving_min + ha.summit_pause_min) AS corrected_min,
                ha.sleep_quality,
                ha.pack_weight_lb,
                ha.trail_condition
            FROM hike_attempts ha
            JOIN activities a ON a.activity_id = ha.activity_id
            WHERE ha.actual_moving_min IS NOT NULL
              AND a.distance_mi > 0
        """).fetchall()
        con.close()

        n = len(rows)
        if n == 0:
            return self.params  # nothing to update

        # Compute observed log(pace) for each attempt
        # pace = (corrected_min - elev_component) / distance_mi
        # We approximate elev_component using current elev_penalty_mu
        log_paces = []
        for dist, elev_ft, corr_min, sleep, pack, cond in rows:
            elev_component = self.params.elev_penalty_mu * (elev_ft / 100.0)
            flat_min = corr_min - elev_component
            if dist > 0 and flat_min > 0:
                log_paces.append(math.log(flat_min / dist))

        if not log_paces:
            return self.params

        log_paces = np.array(log_paces)

        # ── Conjugate normal-normal update ─────────────────────────────────
        # Prior: mu ~ Normal(mu_0, tau_0^2)
        # Likelihood: x_i ~ Normal(mu, sigma^2) — sigma known (prior sigma)
        # Posterior: mu | data ~ Normal(mu_n, tau_n^2)

        mu_0    = PRIOR_MU_LOG_PACE
        tau_0   = PRIOR_SIGMA_LOG_PACE         # prior SD on mu
        sigma   = self.params.sigma_log_pace   # likelihood SD (held fixed for now)
        x_bar   = float(np.mean(log_paces))

        # Posterior precision = prior precision + n * likelihood precision
        prior_prec    = 1.0 / tau_0**2
        likel_prec    = n / sigma**2
        post_prec     = prior_prec + likel_prec
        post_var      = 1.0 / post_prec
        post_mu       = post_var * (prior_prec * mu_0 + likel_prec * x_bar)
        post_sigma    = math.sqrt(post_var)

        # Update sigma estimate (sample std of residuals, shrunk toward prior)
        obs_sigma = float(np.std(log_paces)) if n > 1 else PRIOR_SIGMA_LOG_PACE
        # Weighted average — more data → trust observations more
        weight = min(n / 20.0, 1.0)
        new_sigma = (1 - weight) * PRIOR_SIGMA_LOG_PACE + weight * obs_sigma

        # ── Leave-one-out CV (simple, closed-form for conjugate model) ──────
        loo_errors = []
        for lp in log_paces:
            # LOO posterior excluding this observation
            loo_prec = post_prec - (1.0 / sigma**2)
            loo_var  = 1.0 / loo_prec if loo_prec > 0 else post_var
            loo_mu   = loo_var * (prior_prec * mu_0 + (likel_prec - 1/sigma**2) * x_bar)
            loo_errors.append((lp - loo_mu)**2)
        loo_rmse = math.sqrt(np.mean(loo_errors)) if loo_errors else None

        # ── Persist new posterior ──────────────────────────────────────────
        new_params = ModelParams(
            version_id              = self.params.version_id + 1,
            based_on_n_hikes        = n,
            mu_log_pace             = post_mu,
            sigma_log_pace          = new_sigma,
            elev_penalty_mu         = self.params.elev_penalty_mu,
            elev_penalty_sigma      = self.params.elev_penalty_sigma,
            beta_sleep              = self.params.beta_sleep,
            beta_pack_weight        = self.params.beta_pack_weight,
            beta_trail_snow         = self.params.beta_trail_snow,
            beta_trail_mud          = self.params.beta_trail_mud,
            loo_cv_score            = loo_rmse,
            posterior_predictive_rmse = None,
            notes                   = f"Updated from {n} attempts. x_bar_log={x_bar:.3f}"
        )

        con2 = duckdb.connect(self.db_path)
        con2.execute("""
            INSERT INTO model_params (
                version_id, based_on_n_hikes, mu_log_pace, sigma_log_pace,
                elev_penalty_mu, elev_penalty_sigma,
                beta_sleep, beta_pack_weight, beta_trail_snow, beta_trail_mud,
                loo_cv_score, posterior_predictive_rmse, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            new_params.version_id, new_params.based_on_n_hikes,
            new_params.mu_log_pace, new_params.sigma_log_pace,
            new_params.elev_penalty_mu, new_params.elev_penalty_sigma,
            new_params.beta_sleep, new_params.beta_pack_weight,
            new_params.beta_trail_snow, new_params.beta_trail_mud,
            new_params.loo_cv_score, new_params.posterior_predictive_rmse,
            new_params.notes
        ])
        con2.close()

        self.params = new_params
        print(f"Posterior updated: v{new_params.version_id} | n={n} | "
              f"mu_log_pace={post_mu:.3f} (exp={math.exp(post_mu):.1f} min/mi) | "
              f"LOO RMSE={loo_rmse:.3f}" if loo_rmse else
              f"Posterior updated: v{new_params.version_id} | n={n}")
        return new_params
