"""
collect.py
----------
CLI for all data entry operations:

    python collect.py init                    — create DB + seed hikes table
    python collect.py predict <hike_id>       — show prediction before a hike
    python collect.py log                     — interactive: log a completed attempt
    python collect.py status                  — show completion stats
    python collect.py compare                 — predicted vs actual table

Examples:
    python collect.py init --db hike_tracker.db
    python collect.py predict h11 --sleep 4 --pack 22 --condition dry
    python collect.py log
    python collect.py compare
"""

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
DB_DEFAULT = SCRIPT_DIR.parent / 'data' / 'hike_tracker.db'
SCHEMA_PATH = SCRIPT_DIR.parent / 'schema.sql'


# ── Hike seed data (mirrors the JS tracker) ──────────────────────────────────
HIKE_SEED = [
    ('h1',  'Mount Sanitas',                   'Boulder',           40.021, -105.296, 3.1,  1343, 6863,  'hard',     False, ['warmup','photo']),
    ('h2',  'Royal Arch Trail',                'Boulder/Chautauqua',39.997, -105.281, 3.6,  1400, 6880,  'hard',     False, ['photo','iconic']),
    ('h3',  'South Boulder Peak',              'Boulder',           39.951, -105.293, 9.2,  2400, 8549,  'hard',     False, ['photo','warmup']),
    ('h4',  'Bear Peak',                       'Boulder',           39.966, -105.302, 7.2,  2600, 8461,  'hard',     False, ['photo','iconic']),
    ('h5',  'Eldorado Canyon / Rattlesnake',   'Eldorado Springs',  39.929, -105.286, 4.0,  1000, 6800,  'moderate', False, ['photo','iconic']),
    ('h6',  'Carpenter Peak',                  'Roxborough SP',     39.421, -105.072, 6.2,  1000, 7160,  'moderate', False, ['photo','iconic']),
    ('h7',  'Waterton Canyon → Colorado Trail','Littleton',         39.491, -105.094, 12.6,  650, 8000,  'moderate', False, ['warmup']),
    ('h8',  'South Valley Park',               'Littleton',         39.566, -105.154, 5.0,   400, 6400,  'easy',     False, ['warmup','photo']),
    ('h9',  'Matthews/Winters + Red Rocks',    'Golden/Morrison',   39.694, -105.205, 7.0,   900, 6800,  'moderate', False, ['photo','iconic','warmup']),
    ('h10', 'Mount Falcon',                    'Morrison',          39.632, -105.228, 6.2,  1200, 7851,  'moderate', False, ['photo','warmup']),
    ('h11', 'Grays Peak',                      'Summit County',     39.633, -105.818, 8.0,  3000, 14270, 'hard',     True,  ['14er','photo']),
    ('h12', 'Torreys Peak',                    'Summit County',     39.643, -105.821, 8.0,  3000, 14267, 'hard',     True,  ['14er','photo']),
    ('h13', 'Mount Bierstadt',                 'Clear Creek County',39.583, -105.709, 6.5,  2850, 14060, 'hard',     True,  ['14er','photo']),
    ('h14', 'Quandary Peak',                   'Breckenridge',      39.398, -106.106, 6.75, 3350, 14265, 'hard',     True,  ['14er','photo']),
    ('h15', 'Longs Peak (Keyhole)',            'RMNP',              40.255, -105.616, 14.5, 5100, 14259, 'hard',     True,  ['14er','photo','iconic']),
    ('h16', 'Mount Evans',                     'Clear Creek County',39.589, -105.644, 4.0,  1030, 14264, 'hard',     True,  ['14er','photo']),
    ('h17', 'Chautauqua / Flatirons',          'Boulder',           39.993, -105.281, 4.0,   600, 6500,  'easy',     False, ['photo','iconic']),
    ('h18', 'Herman Gulch',                    'I-70 Corridor',     39.698, -105.874, 6.8,  1700, 12500, 'moderate', False, ['photo','waterfall']),
    ('h19', "St. Mary's Glacier",              'Idaho Springs',     39.807, -105.631, 2.0,   600, 11000, 'easy',     False, ['photo','iconic']),
    ('h20', 'Hanging Lake',                    'Glenwood Canyon',   39.602, -107.189, 2.8,  1010, 7280,  'hard',     False, ['photo','iconic','waterfall']),
    ('h21', 'Maroon Bells Loop',               'Aspen',             39.071, -106.942, 12.0, 2200, 11800, 'hard',     False, ['photo','iconic']),
    ('h22', 'Sky Pond (RMNP)',                 'RMNP',              40.308, -105.680, 9.0,  1780, 10900, 'hard',     False, ['photo','waterfall','iconic']),
    ('h23', 'Pawnee Pass',                     'Indian Peaks',      40.098, -105.621, 12.2, 2700, 12541, 'hard',     False, ['photo']),
    ('h24', 'Spruce Mountain',                 'Larkspur',          39.169, -104.875, 4.6,   450, 7254,  'easy',     False, ['warmup','photo']),
    ('h25', 'Devils Head Fire Tower',          'Pike NF',           39.265, -105.101, 2.8,   940, 9748,  'moderate', False, ['photo','iconic']),
    ('h26', 'Mount Cutler (Cheyenne Canyon)',  'Colorado Springs',  38.784, -104.887, 2.5,   650, 7200,  'moderate', False, ['photo','warmup']),
    ('h27', 'Garden of the Gods Loop',         'Colorado Springs',  38.874, -104.867, 3.5,   300, 6400,  'easy',     False, ['photo','iconic']),
    ('h28', 'Blodgett Peak',                   'Colorado Springs',  38.906, -104.921, 5.0,  2400, 9423,  'hard',     False, ['photo']),
    ('h29', 'James Peak',                      'James Peak Wilderness',39.857,-105.709,8.0, 2300, 13294, 'hard',     False, ['photo','warmup']),
    ('h30', 'Ute Peak',                        'Woodland Park',     38.861, -105.128, 5.5,  1900, 9977,  'moderate', False, ['photo']),
]


def get_db(db_path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path)


def cmd_init(db_path: str):
    print(f"Initializing database: {db_path}")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path)

    with open(SCHEMA_PATH) as f:
        sql = f.read()
    # Strip full-line comments, then split on semicolons
    import re
    sql_clean = re.sub(r'--[^\n]*', '', sql)  # remove all -- comments
    for stmt in sql_clean.split(';'):
        stmt = stmt.strip()
        if stmt:
            try:
                con.execute(stmt)
            except Exception as e:
                if 'already exists' not in str(e).lower():
                    print(f"  Warning: {e}")

    # Seed hikes table
    existing = con.execute("SELECT COUNT(*) FROM hikes").fetchone()[0]
    if existing == 0:
        for row in HIKE_SEED:
            hike_id, name, region, lat, lng, dist, elev, high, diff, is14, tags = row
            con.execute("""
                INSERT INTO hikes (hike_id, name, region, latitude, longitude,
                    distance_mi, elevation_gain_ft, high_point_ft,
                    difficulty, is_14er, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [hike_id, name, region, lat, lng, dist, elev, high, diff, is14, tags])
        print(f"  Seeded {len(HIKE_SEED)} hikes")

    # Seed Naismith prior into model_params
    mp_count = con.execute("SELECT COUNT(*) FROM model_params").fetchone()[0]
    if mp_count == 0:
        con.execute("""
            INSERT INTO model_params (version_id, based_on_n_hikes,
                mu_log_pace, sigma_log_pace, elev_penalty_mu, elev_penalty_sigma,
                beta_sleep, beta_pack_weight, beta_trail_snow, beta_trail_mud,
                notes)
            VALUES (0, 0, ?, 0.25, 1.0, 0.30, 0.0, 0.0, 0.10, 0.05,
                    'Naismith prior — no personal data yet')
        """, [math.log(20.0)])
        print("  Seeded Naismith prior into model_params")

    con.close()
    print("Done. Run `python collect.py status` to verify.")


def cmd_predict(hike_id: str, db_path: str, sleep: int = None,
                pack: float = None, condition: str = None):
    sys.path.insert(0, str(SCRIPT_DIR.parent / 'models'))
    from model import HikePaceModel

    con = get_db(db_path)
    row = con.execute(
        "SELECT name, distance_mi, elevation_gain_ft FROM hikes WHERE hike_id = ?",
        [hike_id]
    ).fetchone()
    con.close()

    if not row:
        print(f"Hike '{hike_id}' not found. Run `python collect.py status` for IDs.")
        return

    name, dist, elev = row
    model = HikePaceModel(db_path)
    pred = model.predict(
        hike_name=name, distance_mi=dist, elevation_gain_ft=int(elev),
        sleep_quality=sleep, pack_weight_lb=pack, trail_condition=condition
    )
    print(pred.summary())
    print(f"\n  Flat: {pred.to_hhmm(pred.flat_component_min)} | "
          f"Elev: +{pred.to_hhmm(pred.elev_component_min)} | "
          f"Cov adj: {pred.covariate_adj_min:+.1f}min")

    # Export samples JSON for the viz app
    out = {
        'hike_id': hike_id,
        'prediction': {
            'median_min':      round(pred.median_min, 1),
            'mean_min':        round(pred.mean_min, 1),
            'lower_90_min':    round(pred.lower_90_min, 1),
            'upper_90_min':    round(pred.upper_90_min, 1),
            'lower_80_min':    round(pred.lower_80_min, 1),
            'upper_80_min':    round(pred.upper_80_min, 1),
            'flat_min':        round(pred.flat_component_min, 1),
            'elev_min':        round(pred.elev_component_min, 1),
            'samples':         [round(s, 2) for s in pred.samples[:500]],  # first 500
            'prior_used':      pred.prior_used,
            'n_trained':       pred.n_hikes_trained_on,
        }
    }
    out_path = SCRIPT_DIR.parent / 'exports' / f'prediction_{hike_id}.json'
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2)
    print(f"\n  Prediction JSON → {out_path}")


def cmd_log(db_path: str):
    """Interactive prompt to log a completed hike attempt."""
    print("\n── Log Hike Attempt ──────────────────────────────────────────")
    con = get_db(db_path)

    hike_id      = input("Hike ID (e.g. h11): ").strip()
    activity_id  = input("Strava Activity ID (or blank to skip): ").strip()
    attempt_date = input(f"Date [{date.today()}]: ").strip() or str(date.today())

    actual_moving  = float(input("Actual moving time (minutes from Strava): "))
    summit_pause   = float(input("Summit pause time you added back (minutes): ") or "0")
    trail_cond     = input("Trail condition [dry/muddy/patchy_snow/snow_covered/icy]: ").strip() or None
    weather        = input("Weather description (free text): ").strip() or None
    temp_f         = input("Temperature °F (blank to skip): ").strip()
    temp_f         = int(temp_f) if temp_f else None
    sleep_q        = input("Sleep quality 1–5 (blank to skip): ").strip()
    sleep_q        = int(sleep_q) if sleep_q else None
    pack_lb        = input("Pack weight lbs (blank to skip): ").strip()
    pack_lb        = float(pack_lb) if pack_lb else None
    rpe            = input("Felt difficulty RPE 1–10 (blank to skip): ").strip()
    rpe            = int(rpe) if rpe else None
    notes          = input("Notes: ").strip() or None
    completed      = input("Reached summit? [Y/n]: ").strip().lower() != 'n'

    # Fetch latest prediction for this hike if it exists
    pred_row = con.execute("""
        SELECT predicted_median_min, predicted_lower_90_min, predicted_upper_90_min, prior_used
        FROM hike_attempts
        WHERE hike_id = ?
        ORDER BY created_at DESC LIMIT 1
    """, [hike_id]).fetchone()

    # Get next attempt_id
    max_id = con.execute("SELECT COALESCE(MAX(attempt_id), 0) FROM hike_attempts").fetchone()[0]
    attempt_id = max_id + 1

    con.execute("""
        INSERT INTO hike_attempts (
            attempt_id, hike_id, activity_id, attempt_date,
            actual_moving_min, summit_pause_min,
            trail_condition, weather_desc, temperature_f,
            sleep_quality, pack_weight_lb, rpe, notes, completed_summit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        attempt_id, hike_id,
        int(activity_id) if activity_id else None,
        attempt_date, actual_moving, summit_pause,
        trail_cond, weather, temp_f,
        sleep_q, pack_lb, rpe, notes, completed
    ])
    con.close()

    print(f"\n  Logged attempt #{attempt_id}")
    corrected = actual_moving + summit_pause
    print(f"  Corrected moving time: {corrected:.0f} min ({corrected/60:.1f}h)")

    # Trigger posterior update
    print("  Updating posterior...")
    sys.path.insert(0, str(SCRIPT_DIR.parent / 'models'))
    from model import HikePaceModel
    model = HikePaceModel(db_path)
    model.update_posterior()


def cmd_status(db_path: str):
    con = get_db(db_path)
    total    = con.execute("SELECT COUNT(*) FROM hikes").fetchone()[0]
    attempts = con.execute("SELECT COUNT(DISTINCT hike_id) FROM hike_attempts WHERE actual_moving_min IS NOT NULL").fetchone()[0]
    model_v  = con.execute("SELECT version_id, based_on_n_hikes, mu_log_pace FROM model_params ORDER BY version_id DESC LIMIT 1").fetchone()

    print(f"\n── Hike Tracker Status ───────────────────────────────────────")
    print(f"  Hikes in catalog:    {total}")
    print(f"  Hikes completed:     {attempts}")
    print(f"  Model version:       v{model_v[0]} ({model_v[1]} hikes trained)")
    print(f"  Current pace prior:  {math.exp(model_v[2]):.1f} min/mi ({60/math.exp(model_v[2]):.1f} mph)")

    print(f"\n  Hike list (ID → name):")
    rows = con.execute("SELECT hike_id, name, difficulty FROM hikes ORDER BY difficulty, hike_id").fetchall()
    for hike_id, name, diff in rows:
        done = con.execute(
            "SELECT COUNT(*) FROM hike_attempts WHERE hike_id = ? AND actual_moving_min IS NOT NULL",
            [hike_id]
        ).fetchone()[0]
        marker = "✓" if done else " "
        print(f"    [{marker}] {hike_id:<6} {name:<40} ({diff})")
    con.close()


def cmd_compare(db_path: str):
    con = get_db(db_path)
    rows = con.execute("""
        SELECT hike_name, attempt_date,
               predicted_median_min, lower_90_min, upper_90_min,  -- wait, use view
               corrected_moving_min, error_min, within_90ci,
               trail_condition, rpe
        FROM v_predictions_vs_actual
        ORDER BY attempt_date DESC
    """).fetchall()

    if not rows:
        print("No completed attempts with predictions yet.")
        con.close()
        return

    print(f"\n{'Hike':<35} {'Date':<12} {'Pred':>7} {'90% CI':>14} {'Actual':>8} {'Error':>7} {'In CI':>6}")
    print("─" * 95)
    for r in rows:
        name, dt, pred, lo, hi, actual, err, in_ci, cond, rpe = r
        ci_str = f"{lo:.0f}–{hi:.0f}" if lo and hi else "—"
        err_str = f"{err:+.0f}m" if err else "—"
        print(f"{str(name):<35} {str(dt):<12} {pred or 0:>7.0f}m {ci_str:>14} {actual or 0:>8.0f}m {err_str:>7} {'yes' if in_ci else 'no':>6}")

    in_ci_pct = sum(1 for r in rows if r[7]) / len(rows) * 100
    print(f"\n  {len(rows)} attempts | {in_ci_pct:.0f}% within 90% CI (target: 90%)")
    con.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='collect.py')
    parser.add_argument('--db', default=str(DB_DEFAULT))
    sub = parser.add_subparsers(dest='cmd')

    sub.add_parser('init')

    p_pred = sub.add_parser('predict')
    p_pred.add_argument('hike_id')
    p_pred.add_argument('--sleep',     type=int,   help='Sleep quality 1-5')
    p_pred.add_argument('--pack',      type=float, help='Pack weight lbs')
    p_pred.add_argument('--condition', type=str,   help='Trail condition')

    sub.add_parser('log')
    sub.add_parser('status')
    sub.add_parser('compare')

    args = parser.parse_args()

    if args.cmd == 'init':
        cmd_init(args.db)
    elif args.cmd == 'predict':
        cmd_predict(args.hike_id, args.db, args.sleep, args.pack, args.condition)
    elif args.cmd == 'log':
        cmd_log(args.db)
    elif args.cmd == 'status':
        cmd_status(args.db)
    elif args.cmd == 'compare':
        cmd_compare(args.db)
    else:
        parser.print_help()
