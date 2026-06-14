#!/usr/bin/env python3
"""
common.py — shared helpers for the TeslaMate drive-recovery toolset.

Universal: nothing car-specific is hardcoded. Everything (car id, slope,
intercept, kWh/%, outage window, location) is discovered from the database or
passed on the command line / read from a per-car calibration file.

Verified against TeslaMate v4.0.1 source (lib/teslamate/log.ex,
lib/teslamate/vehicles/vehicle.ex). Re-check close_drive against your installed
version before trusting the "trap" behaviour — see check_close_drive_guard().

Conventions used throughout:
  * positions.date is stored in UTC, timezone-naive.
  * positions.power and .speed are integers (kW, km/h). power: + = discharge,
    - = regen.
  * battery_level is an integer percent. Range columns are NUMERIC (Decimal).
  * SOC rounding convention: FLOOR. A reading L means true SOC in [L, L+1);
    a downward transition L+1 -> L happens when true SOC crosses (L+1).0, so the
    anchor's true SOC is the HIGHER integer. Calibration regresses range on the
    integer reading and lets the intercept absorb any offset, which is exactly
    consistent with reconstruction at every integer anchor.
"""

import os
import sys
import json
import subprocess
import datetime as dt
from pathlib import Path

# psycopg2 is imported lazily so the pure reconstruction math in this module can
# be imported and unit-tested (see selftest.py) on a machine without the driver.
# It is only required once you actually touch the database.
psycopg2 = None
RealDictCursor = None
execute_values = None


def _require_psycopg2():
    global psycopg2, RealDictCursor, execute_values
    if psycopg2 is not None:
        return
    try:
        import psycopg2 as _p
        from psycopg2.extras import RealDictCursor as _rdc, execute_values as _ev
    except ImportError:
        sys.exit(
            "psycopg2 is required for database access. On the Pi:\n"
            "  python3 -m pip install psycopg2-binary --break-system-packages"
        )
    psycopg2, RealDictCursor, execute_values = _p, _rdc, _ev

# --------------------------------------------------------------------------
# Configuration knobs (overridable via env / CLI in the individual scripts)
# --------------------------------------------------------------------------
DEFAULT_GAP_SECONDS = 300          # parked-between-trips threshold for segmentation
DEFAULT_LOOKBACK_DAYS = 7          # how far back of healthy data calibration may use
DEFAULT_KWH_PER_PCT = 0.55         # last-resort fallback (only for drives w/ no SOC drop)
MIN_R2 = 0.99                      # calibration fit-quality floor
# Outage = a long span where the REST poll died (no range) BUT positions kept
# streaming. Both thresholds matter: a sleeping car also makes a long poll gap,
# but with ~no positions inside it; only an outage has many streamed positions
# with no successful poll. (Tuned against a real streaming-API Model Y backup.)
DEFAULT_MIN_OUTAGE_SECONDS = 1800   # poll gap must exceed 30 min
DEFAULT_MIN_OUTAGE_POSITIONS = 50   # ...and contain this many streamed positions
CALIB_DIR = Path(os.environ.get("TM_CALIB_DIR", Path.home() / ".teslamate_recovery"))

# Docker compose locating, for finalize / pg_dump (TeslaMate standard install).
COMPOSE_DIR = os.environ.get("TM_COMPOSE_DIR", os.getcwd())
DB_SERVICE = os.environ.get("TM_DB_SERVICE", "database")
APP_SERVICE = os.environ.get("TM_APP_SERVICE", "teslamate")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def db_params():
    return dict(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "teslamate"),
        user=os.environ.get("PGUSER", "teslamate"),
        password=os.environ.get("PGPASSWORD") or os.environ.get("DATABASE_PASS"),
    )


def connect():
    _require_psycopg2()
    p = db_params()
    if not p["password"]:
        sys.exit("No DB password. Set PGPASSWORD (or DATABASE_PASS from your .env).")
    conn = psycopg2.connect(**p)
    conn.autocommit = False
    return conn


def fetchall(conn, sql, args=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args or ())
        return cur.fetchall()


def fetchone(conn, sql, args=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args or ())
        return cur.fetchone()


# --------------------------------------------------------------------------
# Car selection
# --------------------------------------------------------------------------
def list_cars(conn):
    return fetchall(conn, "SELECT id, name, vin, model FROM cars ORDER BY id")


def resolve_car(conn, car_arg):
    """Return a car id. Auto-detect when there is exactly one car and --car omitted."""
    cars = list_cars(conn)
    if not cars:
        sys.exit("No cars found in this database.")
    if car_arg is not None:
        ids = {c["id"] for c in cars}
        if int(car_arg) not in ids:
            sys.exit(f"Car {car_arg} not found. Known: {sorted(ids)}")
        return int(car_arg)
    if len(cars) == 1:
        return cars[0]["id"]
    rows = "\n".join(f"  {c['id']}: {c['name']} ({c['model']}, {c['vin']})" for c in cars)
    sys.exit(f"Multiple cars; pass --car <id>:\n{rows}")


# --------------------------------------------------------------------------
# Outage detection
# --------------------------------------------------------------------------
def detect_outage_windows(conn, car_id,
                          min_seconds=DEFAULT_MIN_OUTAGE_SECONDS,
                          min_positions=DEFAULT_MIN_OUTAGE_POSITIONS):
    """
    An outage = a long gap between successful REST polls (no range) during which
    positions KEPT STREAMING. The streamed-position count is what separates an
    outage from a sleeping car (which also makes a long poll gap, but with ~no
    positions inside). Crucially this is NOT just "a run of NULL range" — with the
    streaming API on, normal driving has a NULL-range run between every poll.

    Returns windows sorted by start, each a dict:
      start          first missing (streamed, NULL-range) position date
      end            last missing position date in the window
      last_good_poll date of the last range-bearing poll before the outage
      n              streamed positions inside the window
      soc_lo/soc_hi  integer-SOC span of the missing positions
      first_id/last_id, drive_ids (attached), has_orphan (drive_id NULL present)

    Efficient: only candidate poll-gaps and a few aggregates are pulled, so it is
    fine on a Pi even with millions of positions.
    """
    # candidate gaps between consecutive polls that exceed the duration floor
    gaps = fetchall(
        conn,
        """
        WITH polled AS (
          SELECT date, lag(date) OVER (ORDER BY date) AS prev
          FROM positions
          WHERE car_id = %s AND ideal_battery_range_km IS NOT NULL
        )
        SELECT prev AS lo, date AS hi
        FROM polled
        WHERE prev IS NOT NULL AND EXTRACT(epoch FROM date - prev) >= %s
        """,
        (car_id, min_seconds),
    )
    ranges = [(g["lo"], g["hi"]) for g in gaps]

    # trailing outage: positions after the very last poll that never recovered
    last_poll = fetchone(conn, "SELECT max(date) AS d FROM positions "
                         "WHERE car_id=%s AND ideal_battery_range_km IS NOT NULL",
                         (car_id,))["d"]
    last_any = fetchone(conn, "SELECT max(date) AS d FROM positions WHERE car_id=%s",
                        (car_id,))["d"]
    if last_poll is not None and last_any is not None \
            and (last_any - last_poll).total_seconds() >= min_seconds:
        ranges.append((last_poll, None))

    windows = []
    for lo, hi in ranges:
        agg = fetchone(
            conn,
            """
            SELECT min(date) AS start, max(date) AS end_, count(*) AS n,
                   min(id) AS first_id, max(id) AS last_id,
                   min(battery_level) AS soc_lo, max(battery_level) AS soc_hi,
                   array_remove(array_agg(DISTINCT drive_id), NULL) AS drive_ids,
                   bool_or(drive_id IS NULL) AS has_orphan
            FROM positions
            WHERE car_id = %s AND ideal_battery_range_km IS NULL
              AND date > %s AND (%s::timestamp IS NULL OR date < %s)
            """,
            (car_id, lo, hi, hi),
        )
        if not agg or not agg["n"] or agg["n"] < min_positions:
            continue
        windows.append(dict(
            start=agg["start"], end=agg["end_"], last_good_poll=lo, n=agg["n"],
            first_id=agg["first_id"], last_id=agg["last_id"],
            soc_lo=agg["soc_lo"], soc_hi=agg["soc_hi"],
            drive_ids=sorted(agg["drive_ids"] or []), has_orphan=bool(agg["has_orphan"]),
        ))
    windows.sort(key=lambda w: w["start"])
    return windows


def earliest_outage_start(conn, car_id):
    w = detect_outage_windows(conn, car_id)
    return w[0]["start"] if w else None


# --------------------------------------------------------------------------
# Gap segmentation
# --------------------------------------------------------------------------
def segment_by_gap(positions, gap_seconds=DEFAULT_GAP_SECONDS):
    """
    positions: list of dicts with at least 'date' (datetime), ordered by date.
    Returns a list of segments, each a contiguous sub-list. A new segment starts
    wherever the gap to the previous position exceeds gap_seconds (parked).
    """
    segments, cur = [], []
    for p in positions:
        if cur and (p["date"] - cur[-1]["date"]).total_seconds() > gap_seconds:
            segments.append(cur)
            cur = []
        cur.append(p)
    if cur:
        segments.append(cur)
    return segments


# --------------------------------------------------------------------------
# Reconstruction core (the v2 logic: fixed kWh/% + re-anchor + bucket clamp)
# --------------------------------------------------------------------------
def cumulative_energy(positions):
    """Trapezoidal integral of power (kW) over time -> cumulative kWh per row."""
    E = [0.0] * len(positions)
    for i in range(1, len(positions)):
        dt_s = (positions[i]["date"] - positions[i - 1]["date"]).total_seconds()
        avg_kw = 0.5 * (positions[i]["power"] + positions[i - 1]["power"])
        E[i] = E[i - 1] + avg_kw * dt_s / 3600.0
    return E


def find_anchors(positions, E):
    """Integer-SOC transitions -> anchors at the higher integer (floor convention)."""
    anchors = []
    for i in range(1, len(positions)):
        a, b = positions[i - 1]["soc"], positions[i]["soc"]
        if a is None or b is None or a == b:
            continue
        anchors.append(dict(idx=i, soc=float(max(a, b)), E=E[i]))
    return anchors


def reconstruct_soc(positions, kwh_per_pct_fallback=DEFAULT_KWH_PER_PCT):
    """
    positions: list of dict(date, power, soc[, id]) ordered by date.
    Returns (true_soc list, n_anchors, kwh_per_pct_used).

    Fixed kWh/% for the whole drive (net energy / net integer-SOC drop), re-anchored
    at each integer transition, every value CLAMPED to its own [L, L+1] bucket so a
    data gap or regen swing can never push it more than ~1% (a few km) off, and can
    never invert the mapping.
    """
    n = len(positions)
    if n == 0:
        return [], 0, kwh_per_pct_fallback
    E = cumulative_energy(positions)
    anchors = find_anchors(positions, E)

    s0, sN = positions[0]["soc"], positions[-1]["soc"]
    net_pct = (s0 - sN) if (s0 is not None and sN is not None) else 0
    kwh = (E[-1] - E[0]) / net_pct if net_pct > 0 else kwh_per_pct_fallback
    if kwh <= 0:
        kwh = kwh_per_pct_fallback

    true = [None] * n
    for i in range(n):
        prev = [a for a in anchors if a["idx"] <= i]
        a = prev[-1] if prev else (anchors[0] if anchors else None)
        if a is None:
            s = float(positions[i]["soc"]) + 0.5   # no transitions at all
        else:
            s = a["soc"] - (E[i] - a["E"]) / kwh
        lo = float(positions[i]["soc"])             # reading L -> true in [L, L+1)
        true[i] = min(max(s, lo), lo + 1.0)         # CLAMP to integer bucket
    return true, len(anchors), kwh


def soc_to_range(true_soc, slope, intercept=0.0):
    return [slope * s + intercept for s in true_soc]


# --------------------------------------------------------------------------
# Calibration file (per car)
# --------------------------------------------------------------------------
def calib_path(car_id):
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    return CALIB_DIR / f"calibration_car{car_id}.json"


def save_calibration(car_id, data):
    p = calib_path(car_id)
    p.write_text(json.dumps(data, indent=2, default=str))
    return p


def load_calibration(car_id):
    p = calib_path(car_id)
    if not p.exists():
        sys.exit(f"No calibration for car {car_id} at {p}. Run 2_calibrate.py first.")
    return json.loads(p.read_text())


# --------------------------------------------------------------------------
# Simple ordinary-least-squares with intercept (no numpy dependency)
# --------------------------------------------------------------------------
def linregress(xs, ys):
    """Return (slope, intercept, r2, n). xs, ys are equal-length numeric lists."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return None                       # no spread in x -> slope undefined
    slope = sxy / sxx
    intercept = my - slope * mx
    syy = sum((y - my) ** 2 for y in ys)
    r2 = (sxy ** 2 / (sxx * syy)) if syy > 0 else 1.0
    return slope, intercept, r2, n


# --------------------------------------------------------------------------
# Calibration: SOC -> range slope within the current capacity regime.
# Shared by 2_calibrate.py (saves it) and preview.py (uses it read-only).
# --------------------------------------------------------------------------
def _through_origin(points, ykey):
    """slope = sum(y*soc)/sum(soc^2), intercept 0. Returns dict or None."""
    num = den = 0.0
    n = 0
    for p in points:
        if p[ykey] is None or not p["soc"]:
            continue
        s = float(p["soc"]); y = float(p[ykey])
        num += y * s; den += s * s; n += 1
    if den == 0 or n == 0:
        return None
    return dict(slope=round(num / den, 6), intercept=0.0, n=n)


def _free_regression(points, ykey):
    xs = [float(p["soc"]) for p in points if p[ykey] is not None]
    ys = [float(p[ykey]) for p in points if p[ykey] is not None]
    res = linregress(xs, ys)
    if res is None:
        return None
    slope, intercept, r2, n = res
    return dict(slope=round(slope, 6), intercept=round(intercept, 4), r2=round(r2, 6), n=n)


def last_charge_before(conn, car_id, outage_start):
    """The charge that ended most recently before the outage = current regime start."""
    return fetchone(
        conn,
        """
        SELECT end_date, end_battery_level AS peak_soc, end_ideal_range_km AS peak_range
        FROM charging_processes
        WHERE car_id = %s AND end_date IS NOT NULL AND end_date < %s
          AND end_battery_level IS NOT NULL
        ORDER BY end_date DESC LIMIT 1
        """,
        (car_id, outage_start),
    )


def estimate_kwh_per_pct(conn, car_id, before):
    """Median of (net energy / net integer-SOC drop) over recent healthy drives."""
    import statistics
    drives = fetchall(
        conn,
        "SELECT id FROM drives WHERE car_id = %s AND end_date IS NOT NULL "
        "AND end_date < %s ORDER BY end_date DESC LIMIT 12",
        (car_id, before),
    )
    vals = []
    for d in drives:
        rows = fetchall(conn, "SELECT date, power, battery_level AS soc FROM positions "
                        "WHERE drive_id = %s ORDER BY date", (d["id"],))
        pos = [dict(date=r["date"],
                    power=float(r["power"]) if r["power"] is not None else 0.0,
                    soc=int(r["soc"]) if r["soc"] is not None else None)
               for r in rows if r["soc"] is not None]
        if len(pos) < 3:
            continue
        drop = pos[0]["soc"] - pos[-1]["soc"]
        if drop < 2:
            continue
        E = cumulative_energy(pos)
        k = (E[-1] - E[0]) / drop
        if 0.2 < k < 2.0:
            vals.append(k)
    if vals:
        return round(statistics.median(vals), 4), f"median of {len(vals)} healthy drives"
    return DEFAULT_KWH_PER_PCT, "fallback default (no clean healthy drive found)"


def compute_calibration(conn, car_id, outage_start, lookback_days=DEFAULT_LOOKBACK_DAYS,
                        use_regime=True, allow_intercept=False):
    """
    Fit the SOC->range slope for the current capacity regime. Returns a dict with
    ideal/rated lines, the through-origin slope, the free regression (diagnostic),
    the regime/charge info, a continuity check at the outage boundary, and a
    fallback kWh/%. Raises ValueError if there is too little data to fit.
    """
    lookback_start = outage_start - dt.timedelta(days=lookback_days)
    charge = last_charge_before(conn, car_id, outage_start) if use_regime else None
    if charge and charge["end_date"] > lookback_start:
        window_start = charge["end_date"]
        bound = f"since last charge to {charge['peak_soc']}% (ended {fmt_dt(charge['end_date'])})"
    else:
        window_start = lookback_start
        bound = f"{lookback_days}-day lookback" + ("" if charge else " (no prior charge found)")

    pts = fetchall(
        conn,
        """
        SELECT battery_level AS soc, ideal_battery_range_km AS ideal,
               rated_battery_range_km AS rated
        FROM positions
        WHERE car_id = %s AND ideal_battery_range_km IS NOT NULL
          AND battery_level IS NOT NULL AND date >= %s AND date < %s
        """,
        (car_id, window_start, outage_start),
    )
    if len(pts) < 2:
        raise ValueError("Too few healthy positions in the calibration window. "
                         "Widen --lookback-days or pass --no-regime.")
    socs = [p["soc"] for p in pts]
    soc_lo, soc_hi = min(socs), max(socs)

    diffs = [float(p["ideal"]) - float(p["rated"]) for p in pts if p["rated"] is not None]
    ideal_eq_rated = bool(diffs) and (sum(abs(x) for x in diffs) / len(diffs) < 0.05)

    to_ideal = _through_origin(pts, "ideal")
    free_ideal = _free_regression(pts, "ideal")
    if to_ideal is None or free_ideal is None:
        raise ValueError("Could not fit a slope (no usable points).")
    to_rated = dict(to_ideal) if ideal_eq_rated else _through_origin(pts, "rated")

    if allow_intercept:
        ideal = dict(free_ideal)
        rated = dict(free_ideal) if ideal_eq_rated else _free_regression(pts, "rated")
    else:
        ideal = dict(to_ideal)
        rated = dict(to_ideal) if ideal_eq_rated else dict(to_rated)
    ideal.setdefault("intercept", 0.0)
    rated.setdefault("intercept", 0.0)

    # continuity at the outage boundary
    last = fetchone(
        conn,
        """
        SELECT battery_level AS soc, ideal_battery_range_km AS ideal, date
        FROM positions
        WHERE car_id = %s AND ideal_battery_range_km IS NOT NULL
          AND battery_level IS NOT NULL AND date < %s
        ORDER BY date DESC LIMIT 1
        """,
        (car_id, outage_start),
    )
    continuity = None
    if last:
        pred = ideal["slope"] * float(last["soc"]) + ideal["intercept"]
        actual = float(last["ideal"])
        continuity = dict(soc=last["soc"], actual_km=round(actual, 3),
                          predicted_km=round(pred, 3), residual_km=round(pred - actual, 3),
                          residual_frac=round((pred - actual) / actual, 4) if actual else None,
                          at=fmt_dt(last["date"]))

    kwh, kwh_src = estimate_kwh_per_pct(conn, car_id, outage_start)
    charge_anchor = (round(float(charge["peak_range"]) / float(charge["peak_soc"]), 4)
                     if charge and charge["peak_range"] and charge["peak_soc"] else None)

    return dict(
        car_id=car_id,
        outage_start_utc=fmt_dt(outage_start),
        window_start_utc=fmt_dt(window_start), window_bound=bound,
        lookback_days=lookback_days, n_points=len(pts), soc_span=[soc_lo, soc_hi],
        ideal=ideal, rated=rated, ideal_equals_rated=ideal_eq_rated,
        through_origin_slope=to_ideal["slope"], free_regression=free_ideal,
        charge=(dict(end_date=fmt_dt(charge["end_date"]), peak_soc=charge["peak_soc"],
                     peak_range=float(charge["peak_range"]) if charge["peak_range"] else None)
                if charge else None),
        charge_anchor=charge_anchor,
        kwh_per_pct=kwh, kwh_per_pct_source=kwh_src, continuity=continuity,
    )


# --------------------------------------------------------------------------
# External commands (pg_dump, close_drive rpc) — TeslaMate compose install
# --------------------------------------------------------------------------
def _compose_base():
    return ["docker", "compose"]


def pg_dump(out_path):
    """
    Dump the database via `docker compose exec database pg_dump`. Returns the path.
    Run from the TeslaMate compose directory (TM_COMPOSE_DIR / --compose-dir).
    """
    p = db_params()
    out_path = Path(out_path)
    cmd = _compose_base() + [
        "exec", "-T", DB_SERVICE,
        "pg_dump", "-U", p["user"], p["dbname"],
    ]
    with open(out_path, "wb") as f:
        r = subprocess.run(cmd, cwd=COMPOSE_DIR, stdout=f, stderr=subprocess.PIPE)
    if r.returncode != 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed:\n{r.stderr.decode(errors='replace')}")
    return out_path


def close_drive_rpc(drive_id):
    """Invoke TeslaMate.Log.close_drive/1 on one drive via the app's rpc console."""
    elixir = (
        f"TeslaMate.Repo.get!(TeslaMate.Log.Drive, {int(drive_id)}) "
        f"|> TeslaMate.Log.close_drive()"
    )
    cmd = _compose_base() + ["exec", "-T", APP_SERVICE,
                             "bin/teslamate", "rpc", elixir]
    return subprocess.run(cmd, cwd=COMPOSE_DIR,
                          capture_output=True, text=True)


def check_close_drive_guard():
    """Best-effort reminder; we cannot read the installed source from here."""
    return (
        "Reminder: close_drive DELETES a drive (positions revert to drive_id=NULL) "
        "if no position has BOTH ideal_battery_range_km AND odometer non-null. "
        "Always reconstruct range BEFORE finalize. Verified for v4.0.1."
    )


# --------------------------------------------------------------------------
# CLI / UX helpers
# --------------------------------------------------------------------------
def banner(title, commit):
    bar = "=" * 68
    mode = "[COMMIT — WILL WRITE]" if commit else "[DRY RUN — no writes]"
    print(bar)
    print(f"{title}  {mode}")
    print(bar)


def confirm(prompt):
    try:
        return input(f"{prompt} [type 'yes' to proceed] ").strip().lower() == "yes"
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def fmt_dt(d):
    return d.strftime("%Y-%m-%d %H:%M:%S") if isinstance(d, dt.datetime) else str(d)
