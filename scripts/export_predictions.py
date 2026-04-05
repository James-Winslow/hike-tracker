import duckdb, json, math
from pathlib import Path

con = duckdb.connect(r'data\hike_tracker.db')

params = con.execute("""
    SELECT version_id, based_on_n_hikes, mu_log_pace, sigma_log_pace,
           elev_penalty_mu, elev_penalty_sigma,
           beta_sleep, beta_pack_weight, beta_trail_snow, beta_trail_mud, notes
    FROM model_params ORDER BY version_id DESC LIMIT 1
""").fetchone()

attempts = con.execute("""
    SELECT hike_id, attempt_date, actual_moving_min, summit_pause_min,
           corrected_moving_min, predicted_median_min, rpe, trail_condition, notes
    FROM hike_attempts ORDER BY attempt_date DESC
""").fetchall() if con.execute("SELECT COUNT(*) FROM hike_attempts").fetchone()[0] > 0 else []

con.close()

out = {
    "model": {
        "version_id":         params[0],
        "based_on_n_hikes":   params[1],
        "mu_log_pace":        params[2],
        "sigma_log_pace":     params[3],
        "elev_penalty_mu":    params[4],
        "elev_penalty_sigma": params[5],
        "beta_sleep":         params[6],
        "beta_pack_weight":   params[7],
        "beta_trail_snow":    params[8],
        "beta_trail_mud":     params[9],
        "implied_pace_min_mi": round(math.exp(params[2]), 2),
        "notes": params[10],
    },
    "attempts": [
        {
            "hike_id":             r[0],
            "attempt_date":        str(r[1]),
            "actual_moving_min":   r[2],
            "summit_pause_min":    r[3],
            "corrected_moving_min":r[4],
            "predicted_median_min":r[5],
            "rpe":                 r[6],
            "trail_condition":     r[7],
            "notes":               r[8],
        } for r in attempts
    ]
}

out_path = Path("docs/model_params.json")
out_path.parent.mkdir(exist_ok=True)
with open(out_path, "w") as f:
    json.dump(out, f, indent=2)

print(f"Exported model v{params[0]} ({params[1]} hikes, {round(math.exp(params[2]),1)} min/mi) -> docs/model_params.json")
print(f"Exported {len(attempts)} attempts")
