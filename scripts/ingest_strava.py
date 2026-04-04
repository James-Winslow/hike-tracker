"""
ingest_strava.py
----------------
Parses Strava bulk export (activities.csv) into the activities table.

Usage:
    python ingest_strava.py --csv /path/to/activities.csv --db hike_tracker.db

Strava bulk export: Settings → My Account → Download or Delete Your Account
The zip contains activities.csv plus per-activity GPX/FIT files.

Key columns used from Strava CSV:
    Activity ID, Activity Date, Activity Name, Activity Type,
    Distance, Elapsed Time, Moving Time, Elevation Gain,
    Average Heart Rate, Max Heart Rate, Filename (GPX path)
"""

import argparse
import csv
import re
import duckdb
from datetime import datetime
from pathlib import Path


METERS_TO_FEET = 3.28084
METERS_TO_MILES = 0.000621371
SECONDS_PER_MINUTE = 60


def parse_strava_csv(csv_path: str) -> list[dict]:
    """
    Read Strava activities.csv and normalize units to imperial.
    Returns list of dicts ready for DB insert.
    Strava exports distance in meters, elevation in meters.
    """
    activities = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            activity_type = row.get('Activity Type', '').strip()
            # Only import hike/walk/run — skip rides, swims, etc.
            if activity_type not in ('Hike', 'Walk', 'Run', 'Trail Run'):
                continue

            try:
                activity_id = int(row.get('Activity ID', 0))
                date_str = row.get('Activity Date', '')
                # Strava format: "Jan 15, 2024, 8:30:00 AM"
                dt = _parse_strava_date(date_str)

                dist_m = float(row.get('Distance', 0) or 0)
                elev_m = float(row.get('Elevation Gain', 0) or 0)
                moving_sec = int(float(row.get('Moving Time', 0) or 0))
                elapsed_sec = int(float(row.get('Elapsed Time', 0) or 0))
                avg_hr = _safe_int(row.get('Average Heart Rate'))
                max_hr = _safe_int(row.get('Max Heart Rate'))

                activities.append({
                    'activity_id':       activity_id,
                    'strava_name':       row.get('Activity Name', '').strip(),
                    'activity_type':     activity_type,
                    'activity_date':     dt.date() if dt else None,
                    'start_time':        dt.time() if dt else None,
                    'distance_mi':       round(dist_m * METERS_TO_MILES, 3),
                    'elevation_gain_ft': round(dist_m * METERS_TO_FEET) if False
                                         else round(elev_m * METERS_TO_FEET),
                    'moving_time_sec':   moving_sec,
                    'elapsed_time_sec':  elapsed_sec,
                    'average_hr':        avg_hr,
                    'max_hr':            max_hr,
                    'gpx_filename':      row.get('Filename', '').strip() or None,
                })
            except (ValueError, TypeError) as e:
                print(f"  Skipping row (parse error): {e} | row: {dict(list(row.items())[:4])}")

    return activities


def _parse_strava_date(date_str: str):
    """Handle Strava's inconsistent date formats."""
    formats = [
        '%b %d, %Y, %I:%M:%S %p',
        '%Y-%m-%d %H:%M:%S',
        '%m/%d/%Y %H:%M:%S',
        '%b %d, %Y',
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def _safe_int(val):
    try:
        return int(float(val)) if val and str(val).strip() else None
    except (ValueError, TypeError):
        return None


def ingest(csv_path: str, db_path: str, dry_run: bool = False):
    print(f"Reading: {csv_path}")
    activities = parse_strava_csv(csv_path)
    print(f"Parsed {len(activities)} hike/walk/run activities")

    if dry_run:
        print("\nDRY RUN — first 3 rows:")
        for a in activities[:3]:
            print(f"  {a['activity_date']} | {a['strava_name'][:40]:<40} | "
                  f"{a['distance_mi']:.1f}mi | +{a['elevation_gain_ft']}ft | "
                  f"{a['moving_time_sec']//60}min moving")
        return

    con = duckdb.connect(db_path)

    imported = 0
    skipped = 0
    for act in activities:
        existing = con.execute(
            "SELECT 1 FROM activities WHERE activity_id = ?",
            [act['activity_id']]
        ).fetchone()
        if existing:
            skipped += 1
            continue

        con.execute("""
            INSERT INTO activities (
                activity_id, strava_name, activity_type, activity_date, start_time,
                distance_mi, elevation_gain_ft, moving_time_sec, elapsed_time_sec,
                average_hr, max_hr, gpx_filename
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            act['activity_id'], act['strava_name'], act['activity_type'],
            act['activity_date'], act['start_time'],
            act['distance_mi'], act['elevation_gain_ft'],
            act['moving_time_sec'], act['elapsed_time_sec'],
            act['average_hr'], act['max_hr'], act['gpx_filename']
        ])
        imported += 1

    con.execute("""
        INSERT INTO strava_sync_log (source_file, activities_imported, activities_skipped)
        VALUES (?, ?, ?)
    """, [str(csv_path), imported, skipped])

    con.close()
    print(f"Imported: {imported} | Skipped (already exist): {skipped}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Ingest Strava CSV export')
    parser.add_argument('--csv', required=True, help='Path to Strava activities.csv')
    parser.add_argument('--db', default='hike_tracker.db', help='DuckDB database path')
    parser.add_argument('--dry-run', action='store_true', help='Parse only, no DB writes')
    args = parser.parse_args()
    ingest(args.csv, args.db, args.dry_run)
