#!/usr/bin/env python3
"""
preview.py — READ-ONLY end-to-end dry run. Touches nothing: no DB writes, no
docker, no calibration file. Run this FIRST to check the toolset understands your
data before you commit anything.

It performs the whole recovery in memory and prints, per detected trip, the key
numbers you can sanity-check:
  start/end time, duration, distance, SOC span, the reconstructed start/end range
  and range used, net energy and Wh/km, position counts.

If the trip count, distances, durations and ranges look right here, the real run
(1..5 or run.py) will produce the same drives — it just writes them and lets
TeslaMate's close_drive compute addresses/elevation/aggregates.

Usage:
  PGPASSWORD=... python3 preview.py [--car N] [--gap 300] [--lookback-days 7]
"""

import argparse
import common as c


def source_positions_for_drive(conn, drive_id):
    return _norm(c.fetchall(
        conn,
        """
        SELECT id, date, power, battery_level AS soc, odometer,
               (ideal_battery_range_km IS NULL) AS null_range
        FROM positions WHERE drive_id = %s ORDER BY date
        """,
        (drive_id,)))


def source_positions_orphaned(conn, car_id, w):
    return _norm(c.fetchall(
        conn,
        """
        SELECT id, date, power, battery_level AS soc, odometer,
               (ideal_battery_range_km IS NULL) AS null_range
        FROM positions
        WHERE car_id = %s AND drive_id IS NULL AND date >= %s AND date <= %s
        ORDER BY date
        """,
        (car_id, w["start"], w["end"])))


def _norm(rows):
    return [dict(id=r["id"], date=r["date"],
                 power=float(r["power"]) if r["power"] is not None else 0.0,
                 soc=int(r["soc"]) if r["soc"] is not None else None,
                 odo=float(r["odometer"]) if r["odometer"] is not None else None,
                 null_range=r["null_range"]) for r in rows]


def print_trip_table(segments, cal):
    slope = cal["ideal"]["slope"] if cal else None
    intercept = cal["ideal"]["intercept"] if cal else 0.0
    print(f"\n{'#':>3}  {'start (UTC)':<17}{'end (UTC)':<17}{'min':>5}{'km':>7}"
          f"{'SOC':>9}{'range km (recon)':>18}{'used':>7}{'Wh/km':>7}{'pos':>6}")
    print("-" * 96)
    totals = dict(km=0.0, min=0.0, kwh=0.0, n=0)
    rows_out = []
    for i, seg in enumerate([s for s in segments if len(s) >= 2], 1):
        dur = (seg[-1]["date"] - seg[0]["date"]).total_seconds() / 60.0
        odo0, odo1 = seg[0]["odo"], seg[-1]["odo"]
        dist = (odo1 - odo0) if (odo0 is not None and odo1 is not None) else float("nan")
        E = c.cumulative_energy(seg)
        net_kwh = E[-1] - E[0]
        wh_km = (net_kwh * 1000 / dist) if dist and dist == dist and dist > 0 else float("nan")
        soc = f"{seg[0]['soc']}->{seg[-1]['soc']}"
        if slope is not None:
            true, _, _ = c.reconstruct_soc(seg, cal["kwh_per_pct"])
            r0 = slope * true[0] + intercept
            r1 = slope * true[-1] + intercept
            rng = f"{r0:6.1f}->{r1:6.1f}"
            used = f"{r0 - r1:6.1f}"
        else:
            rng, used = "    n/a", "  n/a"
        print(f"{i:>3}  {c.fmt_dt(seg[0]['date'])[:16]:<17}{c.fmt_dt(seg[-1]['date'])[:16]:<17}"
              f"{dur:>5.0f}{dist:>7.1f}{soc:>9}{rng:>18}{used:>7}{wh_km:>7.0f}{len(seg):>6}")
        totals["km"] += dist if dist == dist else 0
        totals["min"] += dur
        totals["kwh"] += net_kwh
        totals["n"] += 1
    print("-" * 96)
    print(f"  => {totals['n']} trips, {totals['km']:.1f} km, {totals['min']:.0f} min driving, "
          f"{totals['kwh']:.1f} kWh net")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--car", type=int, default=None)
    ap.add_argument("--gap", type=int, default=c.DEFAULT_GAP_SECONDS)
    ap.add_argument("--lookback-days", type=int, default=c.DEFAULT_LOOKBACK_DAYS)
    args = ap.parse_args()

    conn = c.connect()
    try:
        car_id = c.resolve_car(conn, args.car)
        c.banner(f"PREVIEW (read-only, nothing is written)  car={car_id}", commit=False)

        windows = c.detect_outage_windows(conn, car_id)
        if not windows:
            print("\nNo outage detected: no long stretch where the REST poll died while "
                  "positions kept streaming.\nIf you expected one, the data may already be "
                  "fixed, or widen the thresholds. Nothing to do.")
            return
        print(f"\nDetected {len(windows)} outage window(s):")
        for i, w in enumerate(windows, 1):
            dur_h = (w["end"] - w["start"]).total_seconds() / 3600.0
            owners = (", ".join(map(str, w["drive_ids"])) or "orphaned positions")
            print(f"  [{i}] {c.fmt_dt(w['start'])} -> {c.fmt_dt(w['end'])} ({dur_h:.1f} h), "
                  f"{w['n']} positions, SOC {w['soc_hi']}%..{w['soc_lo']}%, drive(s): {owners}")

        # ---- calibration (in memory) ----
        outage_start = windows[0]["start"]
        cal = None
        try:
            cal = c.compute_calibration(conn, car_id, outage_start,
                                        lookback_days=args.lookback_days)
            cont = cal["continuity"]
            print(f"\nCalibration ({cal['window_bound']}):")
            print(f"  slope = {cal['ideal']['slope']} km/% (through origin), "
                  f"fallback {cal['kwh_per_pct']} kWh/%")
            if cont:
                print(f"  continuity at outage boundary: {cont['residual_km']:+.2f} km "
                      f"({100 * cont['residual_frac']:+.2f}%)  "
                      f"{'GOOD' if abs(cont['residual_frac']) < 0.02 else 'CHECK'}")
        except ValueError as e:
            print(f"\nCalibration could not run ({e})\n"
                  f"Showing trips without reconstructed range.")

        # ---- segment + reconstruct each source, in memory ----
        seen_drives = set()
        for w in windows:
            for did in w["drive_ids"]:
                if did in seen_drives:
                    continue
                seen_drives.add(did)
                pos = source_positions_for_drive(conn, did)
                print(f"\nStuck drive {did}: {len(pos)} positions -> trips at "
                      f"{args.gap}s gap:")
                print_trip_table(c.segment_by_gap(pos, args.gap), cal)
            if w["has_orphan"]:
                pos = source_positions_orphaned(conn, car_id, w)
                if pos:
                    print(f"\nOrphaned positions in window {c.fmt_dt(w['start'])}: "
                          f"{len(pos)} -> trips:")
                    print_trip_table(c.segment_by_gap(pos, args.gap), cal)

        print("\n(read-only preview — nothing was written. To apply: run.py --commit, "
              "or the numbered scripts 1..5.)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
