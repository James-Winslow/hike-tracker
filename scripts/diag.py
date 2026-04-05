import duckdb
con = duckdb.connect(r'data\hike_tracker.db')

print('=== All hikes in DB ===')
rows = con.execute("""
    SELECT activity_id, activity_date, strava_name,
           distance_mi, elevation_gain_ft, moving_time_sec/60 as moving_min
    FROM activities
    WHERE activity_type = 'Hike'
    ORDER BY activity_date
""").fetchall()
for r in rows:
    print(f'  {r[1]} | {str(r[2]):<38} | {r[3]}mi | +{r[4]}ft | {r[5]}min')

total = con.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
hikes = con.execute("SELECT COUNT(*) FROM activities WHERE activity_type='Hike'").fetchone()[0]
dates = con.execute("SELECT MIN(activity_date), MAX(activity_date) FROM activities").fetchone()
print(f'\nTotal imported: {total} | Hikes: {hikes} | Range: {dates[0]} to {dates[1]}')

print('\n=== Pace on hikes with real elevation (min/mi) ===')
pace_rows = con.execute("""
    SELECT strava_name, distance_mi, elevation_gain_ft,
           ROUND(moving_time_sec/60.0, 0) as moving_min,
           ROUND((moving_time_sec/60.0) / distance_mi, 1) as pace_min_mi
    FROM activities
    WHERE activity_type = 'Hike'
      AND distance_mi > 2
      AND elevation_gain_ft > 100
    ORDER BY activity_date
""").fetchall()
for r in pace_rows:
    print(f'  {str(r[0]):<38} | {r[1]}mi | +{r[2]}ft | {r[3]:.0f}min | {r[4]} min/mi')

con.close()
