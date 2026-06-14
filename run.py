#!/usr/bin/env python3
"""
run.py — orchestrate the full recovery for one car: diagnose -> pg_dump ->
calibrate -> segment -> reconstruct -> finalize (-> optional weather).

It threads the new drive ids automatically: the drives that 3_segment creates are
detected (as the drive ids that appear after segmentation) and passed explicitly
to 4_reconstruct, 5_finalize and 6_weather — so the pipeline stays correct even
if the car has other NULL-range drives lying around. The stuck drive is
auto-detected (the open drive carrying the streamed-NULL outage); if that's
ambiguous, run the numbered scripts by hand.

DRY-RUN BY DEFAULT: every stage previews. --commit performs the recovery for
real: it takes a pg_dump first, asks for ONE confirmation, then runs the write
steps non-interactively (--yes). Weather is opt-in via --location.

Usage:
  PGPASSWORD=... python3 run.py --car 1                       # full dry-run preview
  PGPASSWORD=... python3 run.py --car 1 --commit \
      --compose-dir /home/pi/teslamate                        # do it for real
  ... add --location "Trier, Germany" to also backfill weather.
"""

import argparse
import sys
import subprocess
import datetime as dt
from pathlib import Path
import common as c

HERE = Path(__file__).resolve().parent


def run_step(script, extra):
    cmd = [sys.executable, str(HERE / script)] + extra
    print(f"\n$ {' '.join(cmd[1:])}\n" + "-" * 68)
    r = subprocess.run(cmd, cwd=c.COMPOSE_DIR)
    if r.returncode != 0:
        sys.exit(f"\nStep {script} failed (rc={r.returncode}). Stopping.")


def drive_ids(car_id):
    conn = c.connect()
    try:
        return {r["id"] for r in c.fetchall(
            conn, "SELECT id FROM drives WHERE car_id=%s", (car_id,))}
    finally:
        conn.close()


def detect_stuck_drive(conn, car_id):
    return c.fetchall(
        conn,
        """
        SELECT d.id, count(p.id) AS n,
               count(p.id) FILTER (WHERE p.ideal_battery_range_km IS NULL) AS nnull
        FROM drives d JOIN positions p ON p.drive_id = d.id
        WHERE d.car_id = %s AND d.end_date IS NULL
        GROUP BY d.id
        HAVING count(p.id) FILTER (WHERE p.ideal_battery_range_km IS NULL) > 0
        ORDER BY nnull DESC
        """,
        (car_id,),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--car", type=int, default=None)
    ap.add_argument("--gap", type=int, default=c.DEFAULT_GAP_SECONDS)
    ap.add_argument("--lookback-days", type=int, default=c.DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--before", default=None)
    ap.add_argument("--weather", action="store_true",
                    help="Also backfill approximate outside_temp (per-drive GPS).")
    ap.add_argument("--location", default=None,
                    help="Weather override: one fixed place for all drives (implies --weather).")
    ap.add_argument("--compose-dir", default=None)
    ap.add_argument("--app-service", default=None)
    ap.add_argument("--yes", action="store_true",
                    help="Skip the single pre-write confirmation (unattended runs).")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    if args.compose_dir:
        c.COMPOSE_DIR = args.compose_dir
    if args.app_service:
        c.APP_SERVICE = args.app_service

    conn = c.connect()
    try:
        car_id = c.resolve_car(conn, args.car)
        stuck = detect_stuck_drive(conn, car_id)
        orphan_windows = [w for w in c.detect_outage_windows(conn, car_id)
                          if w["has_orphan"]]
    finally:
        conn.close()

    car = ["--car", str(car_id)]
    yes = ["--yes"] if args.commit else []
    c.banner(f"RECOVERY ORCHESTRATOR  car={car_id}", commit=args.commit)
    if stuck:
        print("stuck drive candidate(s): "
              + ", ".join(f"{r['id']}({r['nnull']}/{r['n']} null)" for r in stuck))
    elif orphan_windows:
        print(f"no open stuck drive; {len(orphan_windows)} orphaned outage window(s) "
              f"will be segmented from drive_id-NULL positions")
    else:
        print("no stuck open drive and no orphaned positions — assuming the drives are "
              "already split; going straight to reconstruct/finalize")

    # 1) diagnose (always)
    run_step("1_diagnose.py", car)

    # mandatory dump + single confirmation before any write
    if args.commit:
        if len(stuck) > 1:
            sys.exit(f"Multiple stuck-drive candidates {[r['id'] for r in stuck]}; "
                     f"run 3_segment.py --drive <id> manually to disambiguate.")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = Path(c.COMPOSE_DIR) / f"teslamate_backup_{stamp}.sql"
        print(f"\npg_dump -> {out}")
        c.pg_dump(out)
        print("backup OK.")
        if not (args.yes or
                c.confirm("Proceed with the recovery writes (segment, range, close_drive)?")):
            sys.exit("Aborted before any write.")

    # 2) calibrate (writes only the calibration JSON)
    cal = ["--lookback-days", str(args.lookback_days)]
    if args.before:
        cal += ["--before", args.before]
    run_step("2_calibrate.py", car + cal)

    # 3) segment — capture the drive ids it creates
    before_ids = drive_ids(car_id) if args.commit else set()
    if stuck:
        run_step("3_segment.py", car + ["--drive", str(stuck[0]["id"]),
                                        "--gap", str(args.gap)] + yes
                 + (["--commit"] if args.commit else []))
    elif orphan_windows:
        run_step("3_segment.py", car + ["--orphaned", "--gap", str(args.gap)] + yes
                 + (["--commit"] if args.commit else []))
    else:
        print("\n(skipping segmentation — nothing to split)")

    new_ids = sorted(drive_ids(car_id) - before_ids) if args.commit else []
    drives_arg = ["--drives", *map(str, new_ids)] if new_ids else []
    if args.commit and new_ids:
        print(f"\nnew drives created: {new_ids}")

    # 4) reconstruct (explicit ids when we have them, else auto-detect)
    run_step("4_reconstruct.py", car + drives_arg + yes
             + (["--commit"] if args.commit else []))

    # 5) finalize
    fin = car + drives_arg
    if args.compose_dir:
        fin += ["--compose-dir", args.compose_dir]
    if args.app_service:
        fin += ["--app-service", args.app_service]
    run_step("5_finalize.py", fin + yes + (["--commit"] if args.commit else []))

    # 6) optional weather (per-drive GPS by default; --location overrides)
    if args.weather or args.location:
        loc = ["--location", args.location] if args.location else []
        if new_ids:
            run_step("6_weather.py", car + ["--drives", *map(str, new_ids)] + loc
                     + (["--commit"] if args.commit else []))
        else:
            print('\nWeather: re-run after a --commit, or supply ids:\n'
                  f'  python3 6_weather.py {" ".join(car)} --drives <ids> '
                  f'{" ".join(loc)} {"--commit" if args.commit else ""}')

    print("\n" + "=" * 68)
    print("DONE." if args.commit else "DRY-RUN COMPLETE. Re-run with --commit to apply.")
    print("=" * 68)


if __name__ == "__main__":
    main()
