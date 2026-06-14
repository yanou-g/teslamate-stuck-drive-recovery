#!/usr/bin/env python3
"""
2_calibrate.py — fit the per-car SOC->range slope and a fallback kWh/%, then save
the per-car calibration JSON.

The fit logic lives in common.compute_calibration (shared with preview.py). In
brief: range = slope*SOC through the origin, but only WITHIN one battery capacity
regime. On LFP packs the BMS capacity estimate steps up at a high-SOC charge and
drifts down between charges, so we calibrate only over data since the last charge
before the outage (window = max(last_charge_end, outage - N days)) and fit the
slope through the origin (robust to which transition you pick and to range
drifting down while parked). A continuity check at the outage boundary is the
real quality gate.

READ-ONLY on the DB. Writes only the per-car calibration JSON (unless --print).

Usage:
  PGPASSWORD=... python3 2_calibrate.py [--car N] [--lookback-days 7]
        [--before 'YYYY-MM-DD HH:MM:SS'] [--allow-intercept] [--no-regime]
        [--force] [--print]
"""

import argparse
import datetime as dt
import common as c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--car", type=int, default=None)
    ap.add_argument("--lookback-days", type=int, default=c.DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--before", default=None,
                    help="Outage start (UTC). Default: earliest detected outage window.")
    ap.add_argument("--allow-intercept", action="store_true",
                    help="Save the free regression (with intercept) instead of through-origin.")
    ap.add_argument("--no-regime", action="store_true",
                    help="Ignore the last-charge boundary; use the full N-day lookback.")
    ap.add_argument("--force", action="store_true", help="Save even if the fit is poor.")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="Print result without saving the JSON.")
    args = ap.parse_args()

    conn = c.connect()
    try:
        car_id = c.resolve_car(conn, args.car)
        c.banner(f"CALIBRATE  car={car_id}", commit=not args.print_only)

        outage_start = (dt.datetime.fromisoformat(args.before) if args.before
                        else c.earliest_outage_start(conn, car_id))
        if outage_start is None:
            c.sys.exit("No outage window detected and no --before given. Run 1_diagnose.py.")

        try:
            cal = c.compute_calibration(conn, car_id, outage_start,
                                        lookback_days=args.lookback_days,
                                        use_regime=not args.no_regime,
                                        allow_intercept=args.allow_intercept)
        except ValueError as e:
            c.sys.exit(str(e))

        ideal, free = cal["ideal"], cal["free_regression"]
        print(f"outage start (UTC): {cal['outage_start_utc']}")
        print(f"calibration window: {cal['window_start_utc']} .. {cal['outage_start_utc']}")
        print(f"window bounded by : {cal['window_bound']}")
        print(f"healthy points    : {cal['n_points']}  "
              f"SOC {cal['soc_span'][0]}%..{cal['soc_span'][1]}%")
        print(f"\nslope (through origin): {cal['through_origin_slope']} km per 1% SOC")
        print(f"free regression       : {free['slope']} * SOC + {free['intercept']}  "
              f"R^2={free['r2']}   (intercept ~0 => clean single regime)")
        if cal["charge_anchor"] is not None:
            ch = cal["charge"]
            print(f"charge-peak anchor    : {cal['charge_anchor']}  "
                  f"({ch['peak_range']} km at {ch['peak_soc']}%)")
        print(f"SAVING                : ideal = {ideal['slope']} * SOC + {ideal['intercept']}"
              + ("  (with intercept)" if args.allow_intercept else "  (through origin)"))
        print("rated range           : == ideal" if cal["ideal_equals_rated"]
              else f"rated (through origin): {cal['rated']['slope']} * SOC")
        cont = cal["continuity"]
        if cont:
            print(f"continuity @ {cont['soc']}%   : actual {cont['actual_km']} vs predicted "
                  f"{cont['predicted_km']} km (residual {cont['residual_km']:+.2f} km, "
                  f"{100 * cont['residual_frac']:+.2f}%)")
        print(f"fallback kWh/%        : {cal['kwh_per_pct']}  ({cal['kwh_per_pct_source']})")

        # ---- quality gate (continuity first, else R^2) ----
        if abs(free["intercept"]) > 3.0 and not args.allow_intercept:
            print(f"NOTE: free intercept {free['intercept']} km is large — possible regime "
                  f"mixing. Through-origin slope sidesteps it; check the window.")
        refuse = None
        if cont and cont["residual_frac"] is not None:
            rf = abs(cont["residual_frac"])
            if rf > 0.05:
                refuse = f"continuity residual {100 * rf:.1f}% > 5%"
            elif rf > 0.02:
                print(f"WARN: continuity residual {100 * rf:.1f}% (>2%).")
        elif free["r2"] < c.MIN_R2:
            refuse = f"no boundary point and R^2 {free['r2']} < {c.MIN_R2}"
        if refuse and not args.force:
            c.sys.exit(f"REFUSING to save: {refuse}. Inspect the data or use --force.")
        if refuse:
            print(f"WARN (forced): {refuse}")

        cal["generated_utc"] = c.fmt_dt(dt.datetime.now(dt.timezone.utc))
        if args.print_only:
            print("\n(--print: not saved)")
        else:
            p = c.save_calibration(car_id, cal)
            print(f"\nSaved calibration -> {p}")
            print("NEXT: 3_segment.py to split the stuck drive, then 4_reconstruct.py.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
