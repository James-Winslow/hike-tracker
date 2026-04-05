import duckdb, math, statistics

con = duckdb.connect(r'data\hike_tracker.db')

rows = con.execute("""
    SELECT strava_name, distance_mi, elevation_gain_ft,
           moving_time_sec/60.0 as moving_min,
           ROUND((moving_time_sec/60.0) / distance_mi, 2) as pace_min_mi,
           activity_id
    FROM activities
    WHERE activity_type = 'Hike'
      AND distance_mi > 2
      AND elevation_gain_ft > 200
      AND moving_time_sec > 0
    ORDER BY pace_min_mi
""").fetchall()

paces = [r[4] for r in rows]
if len(paces) > 2:
    mu    = statistics.mean(paces)
    sigma = statistics.stdev(paces)
    lo, hi = mu - 2*sigma, mu + 2*sigma
else:
    lo, hi = 0, 999

print(f'Pace stats: mean={statistics.mean(paces):.1f} min/mi  stdev={statistics.stdev(paces):.1f}  2-sigma range=[{lo:.1f}, {hi:.1f}]')
print(f'Log-pace stats: mean(log)={statistics.mean([math.log(p) for p in paces]):.3f}  => exp={math.exp(statistics.mean([math.log(p) for p in paces])):.1f} min/mi\n')

print(f"{'Activity':<42} {'mi':>5} {'ft':>6} {'min':>6} {'min/mi':>7} {'use?':>6}")
print('-' * 75)
for r in rows:
    name, dist, elev, mins, pace, aid = r
    flag = 'YES' if lo <= pace <= hi else 'SKIP'
    print(f'  {str(name):<40} {dist:>5.1f} {elev:>6} {mins:>6.0f} {pace:>7.1f} {flag:>6}')

print(f'\nActivities that will train the model: {sum(1 for r in rows if lo <= r[4] <= hi)}')
print(f'Activities excluded as outliers:      {sum(1 for r in rows if not (lo <= r[4] <= hi))}')
con.close()
