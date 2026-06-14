#!/usr/bin/env python3
"""
1_diagnose.py — READ-ONLY assessment of an API-outage "stuck driving" event.

Reports:
  * open drives (end_date IS NULL) for the car,
  * outage windows (contiguous positions with NULL ideal_battery_range_km while
    positions keep arriving = REST poll dead, stream alive),
  * for each window: time span, position count, which drive(s) own the positions
    (or whether they are orphaned with drive_id NULL), and the integer-SOC span,
  * healthy-data sanity: whether ideal_battery_range_km == rated in this car's
    data, and whether usable_battery_level differs from battery_level (BMS buffer).

Nothing is written. Use the printed window/drive ids to drive 2..5.

Usage:
  PGPASSWORD=... python3 1_diagnose.py [--car N]
"""

import argparse
import common as c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--car", type=int, default=None)
    args = ap.parse_args()

    conn = c.connect()
    try:
        car_id = c.resolve_car(conn, args.car)
        c.banner(f"DIAGNOSE  car={car_id}", commit=False)

        # ---- open drives ----
        open_drives = c.fetchall(
            conn,
            """
            SELECT d.id, d.start_date, d.end_date,
                   count(p.id) AS n_pos,
                   min(p.date) AS first_pos, max(p.date) AS last_pos,
                   count(p.id) FILTER (WHERE p.ideal_battery_range_km IS NULL) AS n_null_range
            FROM drives d
            LEFT JOIN positions p ON p.drive_id = d.id
            WHERE d.car_id = %s AND d.end_date IS NULL
            GROUP BY d.id
            ORDER BY d.start_date
            """,
            (car_id,),
        )
        print(f"\nOPEN DRIVES (end_date IS NULL): {len(open_drives)}")
        for d in open_drives:
            span_h = ((d["last_pos"] - d["first_pos"]).total_seconds() / 3600.0
                      if d["first_pos"] and d["last_pos"] else 0.0)
            flag = "  <-- likely STUCK" if (span_h > 2 and d["n_null_range"] > 0) else ""
            print(f"  drive {d['id']}: start {c.fmt_dt(d['start_date'])}  "
                  f"{d['n_pos']} pos over {span_h:.1f} h  "
                  f"{d['n_null_range']} with NULL range{flag}")
        if not open_drives:
            print("  (none — the stuck drive may already be closed, deleted, or "
                  "split. Check the outage windows below.)")

        # ---- outage windows ----
        windows = c.detect_outage_windows(conn, car_id)
        print(f"\nOUTAGE WINDOWS (long poll-gap with streaming still active): {len(windows)}")
        if not windows:
            print("  None found. No long stretch where the REST poll died while "
                  "positions kept streaming. (Sleeping-car gaps don't count.)")
        for i, w in enumerate(windows, 1):
            dur_h = (w["end"] - w["start"]).total_seconds() / 3600.0
            if w["drive_ids"] and w["has_orphan"]:
                owners = ", ".join(map(str, w["drive_ids"])) + " + orphaned positions"
            elif w["drive_ids"]:
                owners = ", ".join(map(str, w["drive_ids"]))
            else:
                owners = "NONE (orphaned, drive_id NULL)"
            print(f"  [{i}] {c.fmt_dt(w['start'])} -> {c.fmt_dt(w['end'])}  "
                  f"({dur_h:.1f} h)   poll died after {c.fmt_dt(w['last_good_poll'])}")
            print(f"      {w['n']} streamed positions  |  SOC {w['soc_hi']}%..{w['soc_lo']}%  "
                  f"|  owning drive_id(s): {owners}")
            print(f"      position id range: {w['first_id']}..{w['last_id']}")

        # ---- healthy-data sanity (ideal vs rated, buffer) ----
        h = c.fetchone(
            conn,
            """
            SELECT
              count(*) AS n,
              avg(ideal_battery_range_km - rated_battery_range_km) AS ideal_minus_rated,
              avg(battery_level - usable_battery_level)
                FILTER (WHERE usable_battery_level IS NOT NULL) AS buffer_pct,
              min(battery_level) AS soc_lo, max(battery_level) AS soc_hi
            FROM positions
            WHERE car_id = %s AND ideal_battery_range_km IS NOT NULL
            """,
            (car_id,),
        )
        print("\nHEALTHY DATA (positions with range present):")
        if not h or not h["n"]:
            print("  none — cannot calibrate; this car may never have polled range.")
        else:
            imr = float(h["ideal_minus_rated"]) if h["ideal_minus_rated"] is not None else None
            buf = float(h["buffer_pct"]) if h["buffer_pct"] is not None else None
            print(f"  {h['n']} polled positions, SOC {h['soc_lo']}%..{h['soc_hi']}%")
            if imr is not None:
                same = abs(imr) < 0.05
                print(f"  ideal - rated range avg: {imr:+.3f} km  "
                      f"({'ideal == rated (write same value)' if same else 'DIFFER — calibrate both'})")
            if buf is not None:
                print(f"  battery_level - usable_battery_level avg: {buf:+.2f} %  "
                      f"(BMS buffer; intercept in calibration absorbs it)")

        print("\nNEXT: run 2_calibrate.py (uses healthy data before the earliest "
              "outage window), then 3_segment / 4_reconstruct / 5_finalize.")
        print(c.check_close_drive_guard())
    finally:
        conn.close()


if __name__ == "__main__":
    main()
