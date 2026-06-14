#!/usr/bin/env python3
"""
4_reconstruct.py — backfill ideal_battery_range_km + rated_battery_range_km onto
the reconstructed drives' positions, using the per-car calibration.

Method (common.reconstruct_soc): trapezoid-integrate power to cumulative energy,
re-anchor true SOC at every integer-SOC transition, use a FIXED kWh/% (per-drive
net energy / net SOC drop, else the calibration fallback), and CLAMP every value
to its integer bucket [L, L+1]. Then range = slope*SOC + intercept (separate
ideal/rated lines). Writes ONLY where range IS NULL, scoped by drive_id, so real
polled range is never overwritten.

DRY-RUN BY DEFAULT. --commit writes. After committing, run 5_finalize.py.

Modes:
  (default)            reconstruct + (dry-run) report or --commit write
  --validate DRIVE_ID  read-only integration-drift test (single anchor + free
                       integration; reports predicted-vs-actual SOC at each
                       transition — confirms integration quality before trusting it)
  --dump-csv DIR       also write a per-position CSV per drive for inspection

Usage:
  PGPASSWORD=... python3 4_reconstruct.py --drives 885 886 887 [--car N] [--commit]
  PGPASSWORD=... python3 4_reconstruct.py --validate 885
"""

import argparse
import csv
from pathlib import Path
import common as c


def target_drives(conn, car_id, drives_arg):
    if drives_arg:
        return list(drives_arg)
    # auto: drives of this car that still have NULL-range positions
    rows = c.fetchall(
        conn,
        """
        SELECT DISTINCT d.id
        FROM drives d JOIN positions p ON p.drive_id = d.id
        WHERE d.car_id = %s AND p.ideal_battery_range_km IS NULL
        ORDER BY d.id
        """,
        (car_id,),
    )
    return [r["id"] for r in rows]


def fetch_positions(conn, drive_id):
    rows = c.fetchall(
        conn,
        """
        SELECT id, date, power, battery_level AS soc, odometer, speed
        FROM positions WHERE drive_id = %s ORDER BY date
        """,
        (drive_id,),
    )
    return [dict(id=r["id"], date=r["date"],
                power=float(r["power"]) if r["power"] is not None else 0.0,
                soc=int(r["soc"]) if r["soc"] is not None else None,
                odo=float(r["odometer"]) if r["odometer"] is not None else None,
                speed=r["speed"]) for r in rows]


def validate(conn, drive_id, slope):
    """Single-anchor free-integration drift test (read-only)."""
    pos = [p for p in fetch_positions(conn, drive_id) if p["soc"] is not None]
    if len(pos) < 2:
        c.sys.exit("Not enough positions with SOC.")
    E = c.cumulative_energy(pos)
    tr = []
    for i in range(1, len(pos)):
        a, b = pos[i - 1]["soc"], pos[i]["soc"]
        if a is None or b is None or a == b:
            continue
        tr.append(dict(idx=i, soc=float(max(a, b)), E=E[i], date=pos[i]["date"],
                       drop=f"{a}->{b}"))
    if len(tr) < 2:
        c.sys.exit("Need >= 2 SOC transitions to test drift.")
    # Seed kWh/% from the first transition to the next one that is strictly LOWER.
    # (Regen makes integer SOC bounce at a boundary, e.g. 72->71->72->71, so
    # adjacent transitions can share a level; skip those.)
    anchor = tr[0]
    seed_lo = next((t for t in tr[1:] if t["soc"] < anchor["soc"]), None)
    if seed_lo is None:
        c.sys.exit("No pair of transitions with a net SOC drop; cannot seed kWh/%.")
    kwh = (seed_lo["E"] - anchor["E"]) / (anchor["soc"] - seed_lo["soc"])

    def predict(Eh):
        return anchor["soc"] - (Eh - anchor["E"]) / kwh

    print(f"window (UTC): {c.fmt_dt(pos[0]['date'])} -> {c.fmt_dt(pos[-1]['date'])}  "
          f"positions={len(pos)}")
    print(f"anchor: {c.fmt_dt(anchor['date'])}  {anchor['drop']}  true SOC={anchor['soc']:.1f}%")
    print(f"kWh per % (from 1st interval): {kwh:.4f}\n")
    print(f"{'transition (UTC)':<22}{'drop':>8}{'true':>8}{'predicted':>12}{'err %':>9}")
    print("-" * 60)
    max_abs = 0.0
    for t in tr:
        pred = predict(t["E"])
        err = pred - t["soc"]
        max_abs = max(max_abs, abs(err))
        print(f"{c.fmt_dt(t['date']):<22}{t['drop']:>8}{t['soc']:>8.1f}{pred:>12.3f}{err:>+9.3f}")
    print("-" * 60)
    print(f"MAX |error| at any transition: {max_abs:.3f} %  (= {max_abs * slope:.2f} km)")
    print("Small, non-growing -> integration trustworthy. Growing -> power scale/offset "
          "drift; the per-transition re-anchoring in the real run absorbs it anyway.")


def reconstruct_one(conn, drive_id, cal, dump_dir):
    pos = fetch_positions(conn, drive_id)
    if len(pos) < 2 or any(p["soc"] is None for p in pos[:1] + pos[-1:]):
        print(f"drive {drive_id}: <2 positions or missing endpoint SOC — skipped")
        return None

    true, n_anchor, kwh = c.reconstruct_soc(pos, cal["kwh_per_pct"])
    ideal_km = c.soc_to_range(true, cal["ideal"]["slope"], cal["ideal"]["intercept"])
    rated_km = c.soc_to_range(true, cal["rated"]["slope"], cal["rated"]["intercept"])

    E = c.cumulative_energy(pos)
    net_kwh = E[-1] - E[0]
    dist = (pos[-1]["odo"] - pos[0]["odo"]) if (pos[0]["odo"] and pos[-1]["odo"]) else None
    whkm = (net_kwh * 1000 / dist) if dist and dist > 0 else None

    print(f"drive {drive_id}: {len(pos)} pos, {n_anchor} transitions, {kwh:.3f} kWh/%   "
          f"ideal {ideal_km[0]:.1f}->{ideal_km[-1]:.1f} km "
          f"(min {min(ideal_km):.1f}/max {max(ideal_km):.1f})")
    flags = []
    if net_kwh <= 0:
        flags.append("net energy <= 0 (check power sign)")
    if whkm is not None and not (50 < whkm < 600):
        flags.append(f"{whkm:.0f} Wh/km out of typical band")
    if n_anchor == 0:
        flags.append("no SOC transitions — range flat at bucket midpoint")
    if flags:
        print("           FLAGS: " + "; ".join(flags))

    if dump_dir:
        Path(dump_dir).mkdir(parents=True, exist_ok=True)
        out = Path(dump_dir) / f"drive_{drive_id}.csv"
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date_utc", "power_kw", "soc_int", "soc_recon",
                        "ideal_km", "rated_km", "cum_kwh", "odometer_km", "speed"])
            for i, p in enumerate(pos):
                w.writerow([p["date"].isoformat(), p["power"], p["soc"],
                            f"{true[i]:.4f}", f"{ideal_km[i]:.3f}", f"{rated_km[i]:.3f}",
                            f"{E[i]:.4f}", p["odo"] if p["odo"] is not None else "",
                            p["speed"] if p["speed"] is not None else ""])
        print(f"           csv -> {out}")

    # update tuples: (id, ideal, rated) — only NULL-range rows are written
    return [(pos[i]["id"], round(ideal_km[i], 2), round(rated_km[i], 2))
            for i in range(len(pos))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--car", type=int, default=None)
    ap.add_argument("--drives", type=int, nargs="+", default=None,
                    help="Drive ids to reconstruct (default: auto-detect NULL-range drives).")
    ap.add_argument("--validate", type=int, default=None, metavar="DRIVE_ID")
    ap.add_argument("--dump-csv", default=None, metavar="DIR")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = c.connect()
    try:
        car_id = c.resolve_car(conn, args.car)
        cal = c.load_calibration(car_id)

        if args.validate is not None:
            c.banner(f"VALIDATE drift  drive={args.validate}", commit=False)
            validate(conn, args.validate, cal["ideal"]["slope"])
            return

        c.banner(f"RECONSTRUCT  car={car_id}", commit=args.commit)
        print(f"calibration: ideal = {cal['ideal']['slope']}*SOC + "
              f"{cal['ideal']['intercept']}   fallback {cal['kwh_per_pct']} kWh/%\n")

        drives = target_drives(conn, car_id, args.drives)
        if not drives:
            c.sys.exit("No target drives (none with NULL range). Pass --drives explicitly.")

        all_updates = []
        for did in drives:
            ups = reconstruct_one(conn, did, cal, args.dump_csv)
            if ups:
                all_updates.append((did, ups))

        if not args.commit:
            print("\nDRY RUN. min/max should sit within ~1 SOC step (~one slope, a few km) "
                  "of the start/end. Re-run with --commit if the spans look right.")
            return

        if not (args.yes or c.confirm(
                f"Write range to {sum(len(u) for _, u in all_updates)} positions "
                f"across {len(all_updates)} drive(s) (NULL-range only)?")):
            print("Aborted.")
            return

        with conn.cursor() as cur:
            for _, ups in all_updates:
                c.execute_values(
                    cur,
                    "UPDATE positions AS p SET ideal_battery_range_km = v.i, "
                    "rated_battery_range_km = v.r FROM (VALUES %s) AS v(id, i, r) "
                    "WHERE p.id = v.id AND p.ideal_battery_range_km IS NULL",
                    ups, page_size=1000)
        conn.commit()
        print(f"\nCOMMITTED. NEXT: 5_finalize.py --drives "
              + " ".join(str(d) for d, _ in all_updates))
    except Exception:
        conn.rollback()
        print("\nERROR — rolled back, nothing written.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
