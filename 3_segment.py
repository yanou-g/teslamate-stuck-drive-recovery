#!/usr/bin/env python3
"""
3_segment.py — split one stuck drive (or a set of orphaned positions) into the
real individual trips, by time-gap.

Source of positions (pick one):
  --drive N    : split the positions of this (stuck) drive. After reassigning all
                 of them to new per-segment drives, the now-empty drive N is deleted.
  --orphaned   : operate on positions with drive_id IS NULL inside the detected
                 outage window(s) (e.g. a drive a prior botched close_drive deleted).

Segmentation: order by date; a gap > --gap seconds (default 300) starts a new
segment. Each segment with >= 2 positions becomes one new drive (car_id +
start_date only; close_drive fills the rest later).

DRY-RUN BY DEFAULT — prints the trip preview. --commit creates the rows,
reassigns positions, and deletes the emptied stuck drive. Strictly scoped to the
chosen source; healthy drives are never touched.

Usage:
  PGPASSWORD=... python3 3_segment.py --drive 879 [--car N] [--gap 300] [--commit]
  PGPASSWORD=... python3 3_segment.py --orphaned [--car N] [--gap 300] [--commit]
"""

import argparse
import common as c


def fetch_drive_positions(conn, drive_id):
    return c.fetchall(
        conn,
        """
        SELECT id, date, odometer, battery_level AS soc,
               (ideal_battery_range_km IS NULL) AS null_range
        FROM positions WHERE drive_id = %s ORDER BY date
        """,
        (drive_id,),
    )


def fetch_orphaned_positions(conn, car_id):
    windows = c.detect_outage_windows(conn, car_id)
    orphan_windows = [w for w in windows if w["has_orphan"]]
    if not orphan_windows:
        return [], []
    out = []
    for w in orphan_windows:
        out += c.fetchall(
            conn,
            """
            SELECT id, date, odometer, battery_level AS soc,
                   (ideal_battery_range_km IS NULL) AS null_range
            FROM positions
            WHERE car_id = %s AND drive_id IS NULL
              AND date >= %s AND date <= %s
            ORDER BY date
            """,
            (car_id, w["start"], w["end"]),
        )
    out.sort(key=lambda r: r["date"])
    return out, orphan_windows


def preview(segments):
    print(f"\n{len(segments)} segment(s) on the gap threshold:")
    print(f"{'#':>3}  {'start (UTC)':<20}{'end (UTC)':<20}{'min':>6}{'km':>8}"
          f"{'pos':>6}{'SOC':>10}{'nullR':>7}")
    print("-" * 88)
    keep = []
    for i, seg in enumerate(segments, 1):
        dur = (seg[-1]["date"] - seg[0]["date"]).total_seconds() / 60.0
        odo0, odo1 = seg[0]["odometer"], seg[-1]["odometer"]
        dist = (odo1 - odo0) if (odo0 is not None and odo1 is not None) else float("nan")
        nnull = sum(1 for p in seg if p["null_range"])
        soc = f"{seg[0]['soc']}->{seg[-1]['soc']}"
        tiny = len(seg) < 2 or (dist == dist and dist < 0.01)
        flag = "  (too small — will be skipped)" if tiny else ""
        print(f"{i:>3}  {c.fmt_dt(seg[0]['date']):<20}{c.fmt_dt(seg[-1]['date']):<20}"
              f"{dur:>6.0f}{dist:>8.2f}{len(seg):>6}{soc:>10}{nnull:>7}{flag}")
        if not tiny:
            keep.append(seg)
    return keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--car", type=int, default=None)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--drive", type=int, help="Stuck drive id to split.")
    src.add_argument("--orphaned", action="store_true",
                     help="Operate on drive_id-NULL positions in the outage window(s).")
    ap.add_argument("--gap", type=int, default=c.DEFAULT_GAP_SECONDS)
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = c.connect()
    try:
        car_id = c.resolve_car(conn, args.car)
        c.banner(f"SEGMENT  car={car_id}  gap={args.gap}s", commit=args.commit)

        if args.drive is not None:
            d = c.fetchone(conn, "SELECT id, car_id, end_date FROM drives WHERE id=%s",
                           (args.drive,))
            if not d:
                c.sys.exit(f"Drive {args.drive} not found.")
            if d["car_id"] != car_id:
                c.sys.exit(f"Drive {args.drive} belongs to car {d['car_id']}, not {car_id}.")
            positions = fetch_drive_positions(conn, args.drive)
            print(f"source: stuck drive {args.drive} "
                  f"({'OPEN' if d['end_date'] is None else 'closed'}), "
                  f"{len(positions)} positions")
        else:
            positions, ow = fetch_orphaned_positions(conn, car_id)
            print(f"source: orphaned positions in {len(ow)} outage window(s), "
                  f"{len(positions)} positions")

        if len(positions) < 2:
            c.sys.exit("Fewer than 2 positions to segment; nothing to do.")

        segments = c.segment_by_gap(positions, args.gap)
        keep = preview(segments)
        if not keep:
            c.sys.exit("\nNo segment is large enough to be a drive.")

        print(f"\n=> would create {len(keep)} drive(s)"
              + (f" and delete stuck drive {args.drive}" if args.drive else "") + ".")

        if not args.commit:
            print("\nDRY RUN. Re-run with --commit once the trip split looks right.")
            return

        if not (args.yes or c.confirm(f"Create {len(keep)} drives and reassign positions?")):
            print("Aborted.")
            return

        with conn.cursor() as cur:
            new_ids = []
            for seg in keep:
                cur.execute(
                    "INSERT INTO drives (car_id, start_date) VALUES (%s, %s) RETURNING id",
                    (car_id, seg[0]["date"]),
                )
                nid = cur.fetchone()[0]
                new_ids.append(nid)
                ids = [p["id"] for p in seg]
                cur.execute(
                    "UPDATE positions SET drive_id = %s WHERE id = ANY(%s)",
                    (nid, ids),
                )
            if args.drive is not None:
                remaining = c.fetchone(
                    conn, "SELECT count(*) AS n FROM positions WHERE drive_id=%s",
                    (args.drive,))["n"]
                if remaining == 0:
                    cur.execute("DELETE FROM drives WHERE id=%s", (args.drive,))
                    print(f"deleted emptied stuck drive {args.drive}")
                else:
                    print(f"WARN: stuck drive {args.drive} still has {remaining} "
                          f"positions (not all segmented?) — NOT deleting it.")
        conn.commit()
        print(f"\nCOMMITTED. New drive ids: {' '.join(map(str, new_ids))}")
        print("NEXT: 4_reconstruct.py --drives " + " ".join(map(str, new_ids)))
    except Exception:
        conn.rollback()
        print("\nERROR — rolled back, nothing written.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
