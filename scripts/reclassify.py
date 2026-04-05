import duckdb

con = duckdb.connect(r'data\hike_tracker.db')

# These are the activity_ids we want to reclassify.
# Criteria: Walk or Run, >2mi, >500ft gain, pace >15 min/mi
# We'll store the original type before overwriting.

# First add a column to track original type if it doesn't exist
try:
    con.execute("ALTER TABLE activities ADD COLUMN original_type VARCHAR")
    print("Added original_type column")
except Exception as e:
    print(f"Column may already exist: {e}")

# Preview what we're about to reclassify
rows = con.execute("""
    SELECT activity_id, activity_date, strava_name, activity_type,
           distance_mi, elevation_gain_ft,
           ROUND(moving_time_sec/60.0, 0) as moving_min,
           ROUND((moving_time_sec/60.0) / distance_mi, 1) as pace_min_mi
    FROM activities
    WHERE activity_type IN ('Run', 'Walk')
      AND distance_mi > 2
      AND elevation_gain_ft > 500
      AND (moving_time_sec/60.0) / distance_mi > 15
    ORDER BY activity_date
""").fetchall()

print(f"\nActivities to reclassify as Hike ({len(rows)} total):")
print(f"{'Date':<12} {'Name':<42} {'Was':<6} {'mi':>5} {'ft':>6} {'min':>6}")
print('-' * 80)
for r in rows:
    print(f"  {str(r[1]):<12} {str(r[2]):<42} {r[3]:<6} {r[4]:>5.1f} {r[5]:>6} {r[6]:>6.0f}")

confirm = input(f"\nReclassify all {len(rows)} as 'Hike'? [y/n]: ").strip().lower()
if confirm != 'y':
    print("Aborted.")
    con.close()
    exit()

# Save original type, then reclassify
ids = [r[0] for r in rows]
for aid in ids:
    con.execute("""
        UPDATE activities
        SET original_type = activity_type,
            activity_type = 'Hike'
        WHERE activity_id = ?
    """, [aid])

con.commit()
print(f"\nReclassified {len(ids)} activities.")

# Verify final hike count
total_hikes = con.execute("SELECT COUNT(*) FROM activities WHERE activity_type='Hike'").fetchone()[0]
reclassified = con.execute("SELECT COUNT(*) FROM activities WHERE original_type IS NOT NULL").fetchone()[0]
print(f"Total hikes in DB: {total_hikes} ({reclassified} reclassified, {total_hikes - reclassified} originally Hike)")

print("\n=== Full training set pace summary ===")
pace_rows = con.execute("""
    SELECT strava_name, distance_mi, elevation_gain_ft,
           ROUND(moving_time_sec/60.0, 0) as moving_min,
           ROUND((moving_time_sec/60.0) / distance_mi, 1) as pace_min_mi,
           original_type
    FROM activities
    WHERE activity_type = 'Hike'
      AND distance_mi > 2
      AND elevation_gain_ft > 300
      AND moving_time_sec > 0
    ORDER BY pace_min_mi
""").fetchall()

import math, statistics
paces = [r[4] for r in pace_rows]
log_paces = [math.log(p) for p in paces]
print(f"\nN={len(paces)}  mean pace={statistics.mean(paces):.1f} min/mi  stdev={statistics.stdev(paces):.1f}")
print(f"Log-pace: mean={statistics.mean(log_paces):.3f}  stdev={statistics.stdev(log_paces):.3f}")
print(f"exp(mean_log) = {math.exp(statistics.mean(log_paces)):.1f} min/mi  [this seeds our posterior]")
print(f"\nNaismith prior was: 20.0 min/mi  log={math.log(20):.3f}")
print(f"Your actual data:   {math.exp(statistics.mean(log_paces)):.1f} min/mi  log={statistics.mean(log_paces):.3f}")
print(f"Difference: {math.exp(statistics.mean(log_paces)) - 20:.1f} min/mi slower than Naismith")

print(f"\n{'Name':<42} {'mi':>5} {'ft':>6} {'min':>5} {'min/mi':>7} {'was':>5}")
print('-' * 75)
for r in pace_rows:
    was = f"({r[5]})" if r[5] else ""
    print(f"  {str(r[0]):<42} {r[1]:>5.1f} {r[2]:>6} {r[3]:>5.0f} {r[4]:>7.1f} {was:>6}")

con.close()
