# How it works

The technical detail behind the [TeslaMate drive-recovery toolset](README.md):
what an outage actually looks like in the database, how the recovery is done, and
the TeslaMate quirks you need to respect. If you just want to use the tool, the
[README](README.md) is enough — this is for the curious and for anyone adapting it
to a different TeslaMate version.

Verified against **TeslaMate v4.0.1** source (`lib/teslamate/log.ex`,
`lib/teslamate/vehicles/vehicle.ex`). If you run a different version, re-read
`close_drive` in that version first (see [the trap](#the-close_drive-trap) below).

## What an outage looks like in the data

TeslaMate has two ways of getting data from the car:

- the **REST poll**, which records the full picture including
  `ideal_battery_range_km`, `rated_battery_range_km` and `outside_temp`; and
- the **streaming API**, which records lighter `positions` rows (date, lat/lon,
  speed, power, odometer, **integer SOC**, elevation) very frequently while driving.

During a Tesla API outage the streaming API often keeps writing positions while
the REST poll dies. The result:

- range and temperature are **NULL** for the whole outage window, and
- one giant open `drives` row swallows many real, separate trips.

## Detecting the outage

This is the one genuinely subtle part. With the streaming API on, **most positions
have NULL range normally** — there's a NULL-range run between every successful poll
— and a sleeping car leaves long gaps too. So "a run of NULL range" is *not* an
outage on its own.

An outage is defined as **a long gap between successful REST polls that still
contains many streamed positions** — i.e. the car was awake and moving while the
poll was dead. A sleeping car (a long gap with ~no positions inside) is not
flagged. Both thresholds matter, and both are tunable:

- the poll gap must exceed `DEFAULT_MIN_OUTAGE_SECONDS` (30 min), **and**
- it must contain at least `DEFAULT_MIN_OUTAGE_POSITIONS` (50) streamed positions.

The detector pulls only candidate poll-gaps and a few aggregates, so it stays fast
even on a database with millions of positions.

## The `close_drive` trap

`TeslaMate.Log.close_drive/1` does not merely close a drive. Its query
cross-joins a subquery filtered on
`ideal_battery_range_km IS NOT NULL AND odometer IS NOT NULL`. If **no** position
in the drive has *both*, the join is empty and the drive is **deleted** — its
positions revert to `drive_id = NULL` via `on_delete: :nilify_all`. The guard also
requires `count >= 2 AND distance >= 0.01 km`.

→ **You must backfill range onto the positions BEFORE calling `close_drive`.**
`5_finalize.py` pre-flights every drive and refuses to close one that would be
deleted; afterwards it reports each drive as **closed / deleted / not-closed** so a
failed close can't pass silently.

(If a botched `close_drive` already deleted a drive and orphaned its positions,
`3_segment.py --orphaned` rebuilds the trips from the `drive_id = NULL` positions
in the outage window.)

## How calibration works (capacity regime, not "last full charge")

Range is proportional to SOC *through the origin* — but only **within one battery
capacity regime**. The BMS's capacity estimate steps up at a charge (especially a
high-SOC charge on LFP packs, which Tesla asks you to do periodically) and drifts
down between charges, so the slope (`km per 1% SOC`) changes at charge events.
Regressing *across* a charge mixes two regimes and fabricates a phantom intercept.

So `2_calibrate.py`:

- finds the **last charge before the outage** → the start of the current regime;
- takes the window `max(regime_start, outage − N days)` — in-regime *and* recent;
- fits the slope **through the origin** over that window
  (`slope = Σ(range·SOC) / Σ(SOC²)`), which is robust to which transition you pick
  and to range drifting down while parked at high SOC (vampire drain);
- cross-checks with a free regression (its intercept should be ≈0 in one regime —
  a large one flags regime mixing) and the charge-peak anchor;
- gates on a **continuity check**: the slope, applied to the last real SOC before
  the outage, must reproduce that point's actual range (residual reported).

`--allow-intercept` saves the free regression instead (for a non-LFP pack with a
real reserve offset); `--no-regime` ignores the charge boundary. A flat-SOC window
still yields a slope from the single highest-SOC point.

## How range is reconstructed

Per-position SOC during the outage is reconstructed by integrating `power` over
time with a **fixed kWh/%** (per-drive net energy ÷ net integer-SOC drop, falling
back to a calibrated default), **re-anchored at every integer-SOC transition**, and
**clamped to each reading's `[L, L+1]` bucket**. So a data gap or a regen swing can
never shift a value more than ~1% (a few km) or invert the SOC mapping. Range is
then `slope · SOC + intercept`, with separate ideal/rated lines.

Writes are scoped: range is written only `WHERE ideal_battery_range_km IS NULL` and
only for the targeted drives, so real polled range is never overwritten and healthy
drives are never touched.

## Safety properties (summary)

- **Dry-run by default**, explicit `--commit` to write, `pg_dump` before the first
  write via `run.py --commit`.
- **Scoped writes:** only NULL range/temperature on the targeted recovered drives.
- **`close_drive` only after range exists**, with a pre-flight that refuses to
  finalize a drive that would be deleted, and per-drive closed/deleted/not-closed
  reporting afterwards.
- **Calibration refuses a poor fit** unless you pass `--force`: the gate is the
  continuity residual at the outage boundary (>5% refuses, >2% warns), falling back
  to `R² < 0.99` when there is no boundary point to check against.

## Testing it safely

Three escalating checks, each safe:

1. **Offline math** — no DB: `python3 selftest.py`
2. **Read-only on your data** — `python3 preview.py`: detects the outage,
   calibrates, and prints the trips it *would* create, writing nothing.
3. **Full rehearsal on a copy** — restore your `pg_dump` into a throwaway Postgres
   + TeslaMate stack and run the whole thing there before touching your live DB. A
   ready-made isolated stack is in [`docker_test/`](docker_test/) (Postgres on host
   port 15432; `docker compose -p tmrecovery up -d`, restore your dump, point the
   scripts at `PGPORT=15432`). Tear down with
   `docker compose -p tmrecovery down -v`.

## Files

| File | Role |
|------|------|
| `common.py` | DB, car detect, outage detection, gap segmentation, reconstruction math, regime calibration, calibration I/O, `pg_dump`/`close_drive` helpers |
| `preview.py` | **Read-only** whole-pipeline dry run → per-trip key data (run this first) |
| `1_diagnose.py` | Read-only assessment |
| `2_calibrate.py` | Fit the regime SOC→range slope (through-origin) + fallback kWh/% → per-car JSON |
| `3_segment.py` | Split the stuck drive / orphaned positions |
| `4_reconstruct.py` | Backfill range (+ `--validate`, `--dump-csv`) |
| `5_finalize.py` | `close_drive` each drive safely |
| `6_weather.py` | Optional approximate `outside_temp` |
| `run.py` | Orchestrate the whole pipeline |
| `selftest.py` | Offline unit tests for the reconstruction math |
| `docker_test/` | Isolated throwaway TeslaMate stack to rehearse against a DB backup |

## Re-verifying against your TeslaMate version

The `close_drive` behaviour was verified against TeslaMate's own source at tag
`v4.0.1`. That source is **not committed** (it's gitignored — bloat, and it's
TeslaMate's code); clone it yourself to re-verify against your version:

```bash
git clone --depth 1 --branch v4.0.1 https://github.com/teslamate-org/teslamate
```

## Notes

- The per-car calibration JSON is written under `~/.teslamate_recovery/` (override
  with `TM_CALIB_DIR`).
- Service names default to `database` / `teslamate`; override with `TM_DB_SERVICE`
  / `TM_APP_SERVICE` if yours differ.
- **Never commit your DB dump** — it contains your VIN, tokens and full location
  history. The `.gitignore` excludes `*.sql*`.
