import duckdb

con = duckdb.connect(r'data\hike_tracker.db')

# Find walks and runs that have hike-like characteristics:
# slow pace (>15 min/mi), meaningful elevation (>500ft), meaningful distance (>2mi)
print("=== Walks/Runs that are probably hikes ===")
print(f"{'Date':<12} {'Name':<40} {'Type':<10} {'mi':>5} {'ft':>6} {'min':>6} {'min/mi':>7}")
print('-' * 90)

rows = con.execute("""
    SELECT activity_date, strava_name, activity_type,
           distance_mi, elevation_gain_ft,
           ROUND(moving_time_sec/60.0, 0) as moving_min,
           ROUND((moving_time_sec/60.0) / distance_mi, 1) as pace_min_mi,
           activity_id
    FROM activities
    WHERE activity_type IN ('Run', 'Walk')
      AND distance_mi > 2
      AND elevation_gain_ft > 500
      AND (moving_time_sec/60.0) / distance_mi > 15
    ORDER BY activity_date
""").fetchall()

for r in rows:
    print(f"  {str(r[0]):<12} {str(r[1]):<40} {r[2]:<10} {r[3]:>5.1f} {r[4]:>6} {r[5]:>6.0f} {r[6]:>7.1f}")

print(f"\nProbable hikes mislabeled: {len(rows)}")

print("\n=== All walks > 3mi with any elevation ===")
walks = con.execute("""
    SELECT activity_date, strava_name,
           distance_mi, elevation_gain_ft,
           ROUND(moving_time_sec/60.0, 0) as moving_min,
           ROUND((moving_time_sec/60.0) / distance_mi, 1) as pace_min_mi
    FROM activities
    WHERE activity_type = 'Walk'
      AND distance_mi > 3
    ORDER BY elevation_gain_ft DESC
    LIMIT 20
""").fetchall()

for r in walks:
    print(f"  {str(r[0]):<12} {str(r[1]):<40} {r[2]:>5.1f}mi {r[3]:>6}ft {r[4]:>5.0f}min {r[5]:>6.1f} min/mi")

con.close()
