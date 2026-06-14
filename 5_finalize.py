#!/usr/bin/env python3
"""
5_finalize.py — close each reconstructed drive via TeslaMate's own
TeslaMate.Log.close_drive/1 (run in the app container), so it natively computes
distance, duration, speed/power extremes, start/end range, addresses, geofences,
elevation ascent/descent, and temp averages — exactly like a normal drive.

SAFETY: close_drive DELETES a drive (positions revert to drive_id=NULL) if no
position in it has BOTH ideal_battery_range_km AND odometer non-null, or if
count < 2 or distance < 0.01 km. We pre-flight every drive for this and refuse to
finalize one that would be deleted (run 4_reconstruct.py first). Verified against
TeslaMate v4.0.1 lib/teslamate/log.ex.

This step calls `docker compose exec`, so run it on the host where TeslaMate's
compose project lives (the Pi). Set the compose dir with --compose-dir or
TM_COMPOSE_DIR, and the app service with --app-service or TM_APP_SERVICE.

DRY-RUN BY DEFAULT (pre-flight only). --commit actually runs close_drive.

Usage:
  python3 5_finalize.py --drives 885 886 887 --compose-dir /home/pi/teslamate --commit
"""

import argparse
import common as c


def preflight(conn, drive_id):
    r = c.fetchone(
        conn,
        """
        SELECT count(*) AS n,
               count(*) FILTER (WHERE ideal_battery_range_km IS NOT NULL
                                  AND odometer IS NOT NULL) AS n_both,
               max(odometer) - min(odometer) AS dist
        FROM positions WHERE drive_id = %s
        """,
        (drive_id,),
    )
    n = r["n"] or 0
    n_both = r["n_both"] or 0
    dist = float(r["dist"]) if r["dist"] is not None else 0.0
    would_delete = (n_both == 0) or (n < 2) or (dist < 0.01)
    reason = ("no position has BOTH range and odometer" if n_both == 0
              else "fewer than 2 positions" if n < 2
              else "distance < 0.01 km" if dist < 0.01 else "")
    return dict(n=n, n_both=n_both, dist=dist, would_delete=would_delete, reason=reason)


def auto_drives(conn, car_id):
    rows = c.fetchall(
        conn,
        """
        SELECT d.id
        FROM drives d
        WHERE d.car_id = %s AND d.end_date IS NULL
          AND EXISTS (SELECT 1 FROM positions p WHERE p.drive_id = d.id)
          AND (SELECT max(date) FROM positions p WHERE p.drive_id = d.id)
              < now() - interval '10 minutes'
        ORDER BY d.id
        """,
        (car_id,),
    )
    return [r["id"] for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--car", type=int, default=None)
    ap.add_argument("--drives", type=int, nargs="+", default=None)
    ap.add_argument("--compose-dir", default=None)
    ap.add_argument("--app-service", default=None)
    ap.add_argument("--force", action="store_true",
                    help="Run close_drive even on drives that would be deleted.")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    if args.compose_dir:
        c.COMPOSE_DIR = args.compose_dir
    if args.app_service:
        c.APP_SERVICE = args.app_service

    conn = c.connect()
    try:
        car_id = c.resolve_car(conn, args.car)
        c.banner(f"FINALIZE  car={car_id}", commit=args.commit)
        print(c.check_close_drive_guard() + "\n")

        drives = args.drives or auto_drives(conn, car_id)
        if not drives:
            c.sys.exit("No target drives. Pass --drives (recommended) from step 4.")
        if not args.drives:
            print(f"auto-detected open, settled drives: {drives}\n")

        safe = []
        for did in drives:
            pf = preflight(conn, did)
            status = (f"WOULD BE DELETED ({pf['reason']})" if pf["would_delete"]
                      else "ok")
            print(f"drive {did}: {pf['n']} pos, {pf['n_both']} with range+odo, "
                  f"{pf['dist']:.2f} km  ->  {status}")
            if not pf["would_delete"]:
                safe.append(did)

        run_set = drives if args.force else safe
        skipped = [d for d in drives if d not in run_set]
        if skipped:
            print(f"\nSKIPPING (would be deleted; run 4_reconstruct first): {skipped}")

        if not args.commit:
            print(f"\nDRY RUN. {len(run_set)} drive(s) ready to close: {run_set}")
            print("Re-run with --commit (on the Pi) to run close_drive.")
            return
        if not run_set:
            c.sys.exit("Nothing safe to finalize.")
        if not (args.yes or c.confirm(f"Run close_drive on {len(run_set)} drive(s) {run_set}?")):
            print("Aborted.")
            return

        failures = 0
        for did in run_set:
            res = c.close_drive_rpc(did)
            tail = (res.stderr or res.stdout or "").strip().splitlines()
            note = tail[-1] if tail else f"(rc={res.returncode})"
            still = c.fetchone(conn, "SELECT distance, duration_min, end_date "
                               "FROM drives WHERE id=%s", (did,))
            if still is None:
                failures += 1
                print(f"drive {did}: DELETED by close_drive ({note}). "
                      f"Range backfill was missing — re-run 4_reconstruct.")
            elif still["end_date"] is None:
                # rpc didn't actually close it: bad rc, app down, or stale DB conn
                failures += 1
                print(f"drive {did}: NOT CLOSED (still open). close_drive did not run — "
                      f"check the app container is up and connected. rpc said: {note}")
            else:
                print(f"drive {did}: closed  {float(still['distance'] or 0):.2f} km, "
                      f"{still['duration_min']} min, end {c.fmt_dt(still['end_date'])}")
        if failures:
            print(f"\n{failures} drive(s) were NOT closed — see above. Fix the cause and "
                  f"re-run 5_finalize.py on those ids.")
        else:
            print("\nDone. Check the Drives dashboard. "
                  "Optional: 6_weather.py for outside temperature.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
