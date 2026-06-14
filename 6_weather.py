#!/usr/bin/env python3
"""
6_weather.py — OPTIONAL, opt-in approximate outside-temperature backfill.

The car's own outside_temp sensor wasn't logged during the outage (it comes from
the REST poll), so this fills an APPROXIMATION from historical ambient air temp
(Open-Meteo, free, no API key): hourly temperature linearly interpolated to each
position's timestamp. It writes positions.outside_temp (so it survives a future
close_drive recompute) and sets drives.outside_temp_avg.

By default each drive uses ITS OWN location — the median GPS point of the drive's
positions — so a trip that crossed regions gets locally-correct temperatures. All
the drives' locations are fetched in a single Open-Meteo request. Pass --location
"City, Country" to instead use one fixed geocoded point for every drive (useful
only if the positions have no GPS).

Clearly labelled ambient air temperature, not sensor data.

DRY-RUN BY DEFAULT. --commit writes. Only NULL-temp positions are touched.

Usage:
  PGPASSWORD=... python3 6_weather.py --drives 885 886 887 [--car N] [--commit]
  PGPASSWORD=... python3 6_weather.py --drives 885 886 887 --location "Trier, Germany"
"""

import argparse
import json
import time
import datetime as dt
import urllib.request
import urllib.parse
import common as c

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&format=json"
FORECAST = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m&past_days={pd}&forecast_days=1&timezone=GMT")
ARCHIVE = ("https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}"
           "&start_date={s}&end_date={e}&hourly=temperature_2m&timezone=GMT")


def geocode(location):
    url = GEOCODE.format(q=urllib.parse.quote(location))
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    res = (data.get("results") or [])
    if not res:
        c.sys.exit(f"Could not geocode '{location}'.")
    top = res[0]
    print(f"location (override): {top.get('name')}, {top.get('country', '')}  "
          f"{top['latitude']:.4f},{top['longitude']:.4f}  (used for all drives)")
    return (float(top["latitude"]), float(top["longitude"]))


def drive_location(conn, drive_id):
    """Median (lat, lon) of a drive's positions, or None if it has no GPS."""
    r = c.fetchone(
        conn,
        """
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latitude)  AS lat,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY longitude) AS lon
        FROM positions WHERE drive_id = %s AND latitude IS NOT NULL
        """,
        (drive_id,),
    )
    if not r or r["lat"] is None:
        return None
    return (float(r["lat"]), float(r["lon"]))


def load_weather(coords, span_lo, span_hi, force_archive, batch=100):
    """
    coords: list of (lat, lon). Returns a list of series (one per coord, same
    order), each a sorted list of (epoch_utc, temp_c).

    Coordinates are sent in batches of `batch` per request (so up to 100 drives =
    one request), keeping URLs short and staying a tiny, polite load on
    Open-Meteo's keyless free tier no matter how many drives there are.
    """
    today = dt.datetime.now(dt.timezone.utc).date()
    days_back = (today - span_lo.date()).days
    use_archive = force_archive or days_back > 7
    series_list = []
    n_req = 0
    for start in range(0, len(coords), batch):
        chunk = coords[start:start + batch]
        lats = ",".join(f"{a:.4f}" for a, _ in chunk)
        lons = ",".join(f"{b:.4f}" for _, b in chunk)
        if use_archive:
            url = ARCHIVE.format(lat=lats, lon=lons,
                                 s=span_lo.date().isoformat(), e=span_hi.date().isoformat())
        else:
            url = FORECAST.format(lat=lats, lon=lons, pd=min(max(days_back + 1, 1), 92))
        if n_req:
            time.sleep(1)                         # be polite between batches
        with urllib.request.urlopen(url, timeout=45) as r:
            data = json.load(r)
        n_req += 1
        items = data if isinstance(data, list) else [data]  # multi -> list, single -> object
        for it in items:
            times = it["hourly"]["time"]
            temps = it["hourly"]["temperature_2m"]
            s = [(dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp(),
                  float(v)) for t, v in zip(times, temps) if v is not None]
            s.sort()
            series_list.append(s)
    print(f"weather: {'archive' if use_archive else 'forecast'} endpoint, "
          f"{len(coords)} location(s) in {n_req} request(s), "
          f"{len(series_list[0]) if series_list else 0} hourly points each")
    return series_list


def interp(series, when_naive_utc):
    e = when_naive_utc.replace(tzinfo=dt.timezone.utc).timestamp()
    if e <= series[0][0]:
        return series[0][1]
    if e >= series[-1][0]:
        return series[-1][1]
    lo, hi = 0, len(series) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if series[mid][0] <= e:
            lo = mid
        else:
            hi = mid
    (e0, t0), (e1, t1) = series[lo], series[hi]
    return t0 + (t1 - t0) * (e - e0) / (e1 - e0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--car", type=int, default=None)
    ap.add_argument("--drives", type=int, nargs="+", required=True)
    ap.add_argument("--location", default=None,
                    help='Override: one fixed place for all drives, e.g. "Trier, Germany". '
                         "Default: each drive's own median GPS point.")
    ap.add_argument("--archive", action="store_true", help="Force the archive endpoint.")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = c.connect()
    try:
        car_id = c.resolve_car(conn, args.car)
        c.banner(f"WEATHER (approx outside_temp)  car={car_id}", commit=args.commit)

        span = c.fetchone(conn, "SELECT min(date) AS lo, max(date) AS hi FROM positions "
                          "WHERE drive_id = ANY(%s)", (args.drives,))
        if not span or span["lo"] is None:
            c.sys.exit("No positions for those drives.")

        # one location for all (override) or each drive's own median GPS (default)
        loc_by_drive = {}
        if args.location:
            shared = geocode(args.location)
            series = load_weather([shared], span["lo"], span["hi"], args.archive)[0]
            if not series:
                c.sys.exit("Weather API returned no usable data for that span.")
            series_by_drive = {did: series for did in args.drives}
            loc_by_drive = {did: shared for did in args.drives}
        else:
            located = []
            for did in args.drives:
                loc = drive_location(conn, did)
                loc_by_drive[did] = loc
                if loc:
                    located.append((did, loc))
            if not located:
                c.sys.exit("None of those drives have GPS positions. Pass --location.")
            series_list = load_weather([l for _, l in located], span["lo"], span["hi"],
                                       args.archive)
            series_by_drive = {did: series_list[i] for i, (did, _) in enumerate(located)}

        with conn.cursor() as cur:
            for did in args.drives:
                series = series_by_drive.get(did)
                loc = loc_by_drive.get(did)
                where = f"@{loc[0]:.3f},{loc[1]:.3f}" if loc else "no GPS"
                if not series:
                    print(f"drive {did}: {where} — no weather/location, skipped")
                    continue
                rows = c.fetchall(conn, "SELECT id, date FROM positions WHERE drive_id=%s "
                                  "AND outside_temp IS NULL ORDER BY date", (did,))
                if not rows:
                    print(f"drive {did}: {where} — no NULL-temp positions, skipped")
                    continue
                ups = [(r["id"], round(interp(series, r["date"]), 1)) for r in rows]
                avg_t = sum(t for _, t in ups) / len(ups)
                print(f"drive {did}: {where}  {len(ups)} pos, "
                      f"{ups[0][1]:.1f}..{ups[-1][1]:.1f} C, avg {avg_t:.1f} C")
                if args.commit:
                    c.execute_values(
                        cur,
                        "UPDATE positions AS p SET outside_temp = v.t "
                        "FROM (VALUES %s) AS v(id, t) "
                        "WHERE p.id = v.id AND p.outside_temp IS NULL",
                        ups, page_size=1000)
                    cur.execute("UPDATE drives SET outside_temp_avg=%s WHERE id=%s",
                                (round(avg_t, 1), did))
        if args.commit:
            conn.commit()
            print("\nCOMMITTED. Refresh the Drives page — Temp should now show "
                  "(approximate ambient air temperature).")
        else:
            print("\nDRY RUN. Re-run with --commit to write.")
    except Exception:
        conn.rollback()
        print("\nERROR — rolled back, nothing written.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
