# TeslaMate drive-recovery toolset

Recover the trips TeslaMate loses when a Tesla API outage leaves your car
**stuck in `driving`**.

When Tesla's API goes down (403 errors etc. like we saw mid June 2026), the streaming API keeps working, so
TeslaMate keeps logging GPS positions but stops recording battery range, temperatures etc. The
result: your car shows as **driving** for hours or days, and a whole run of
separate real trips gets swallowed into one giant, never-ending "drive" with no
range, and no distance, duration, temperature, etc.

This toolset splits that mess back into the individual trips it should have been,
fills in the missing battery range, and closes each trip the same way TeslaMate
normally would — so they show up correctly in your dashboards.

**This might be for you if:**

- Your car has been stuck showing as **driving** for hours or days.
- The trips you actually took are missing — swallowed into that one never-ending drive.
- It lines up with a known Tesla API or TeslaMate outage.

Nothing is car-specific or hardcoded — everything (your car, the outage window,
the battery calibration, the location) is detected from your own database.

> ⚠️ **This writes to your TeslaMate database.** It is **read-only until you add
> `--commit`**, and it takes a database backup before its first write — but you are
> editing real data. **Take your own backup first** and run the read-only preview
> before committing.
>
> Unofficial community tooling — **not affiliated with or endorsed by the TeslaMate
> project**. No warranty; use at your own risk.

---

## What you need

- The machine where TeslaMate runs (your Raspberry Pi, NAS, server, etc.) and a
  **terminal** on it. These are command-line scripts — there's no app to click.
- **Python 3.8+** and the `psycopg2` database driver (installed below).
- Access to your TeslaMate **PostgreSQL** database (you already have this if you
  run TeslaMate).
- For the final step, **`docker compose`** access to your TeslaMate stack — which
  is why you run these on the same machine as TeslaMate.

---

> _Built with [Claude Code](https://claude.com/claude-code). So far this has only been tested on the author's own TeslaMate database and hasn't been independently verified by anyone else — please read the code and back up your database before running it._

## Quick start

Run these in a **terminal on the machine where TeslaMate is installed**.

**1. Get the code.** If you have `git`, clone it:

```bash
git clone https://github.com/yanou-g/teslamate-stuck-drive-recovery.git
cd teslamate-stuck-drive-recovery
```

No `git`? On the project's GitHub page click **Code → Download ZIP**, unzip it,
then `cd` into the unzipped folder.

**2. Install the one dependency:**

```bash
python3 -m pip install psycopg2-binary --break-system-packages
```

**3. Tell the scripts how to reach your database.** These are TeslaMate's
defaults; `PGPASSWORD` is the `DATABASE_PASS` value from your TeslaMate `.env`
file:

```bash
export PGHOST=127.0.0.1 PGPORT=5432 PGDATABASE=teslamate PGUSER=teslamate
export PGPASSWORD=your-database-password
```

**4. See what it would do.** This only reads — it writes nothing. Always run it
first:

```bash
python3 preview.py
```

**5. If the trips, distances and ranges look right, do it for real.** This backs
up the database first, then asks once before writing anything:

```bash
python3 run.py --commit --compose-dir /path/to/teslamate
```

That's it. `run.py` runs the whole recovery in order — detect the outage,
calibrate, split the stuck drive into real trips, fill in the range, and close
each trip — and prints what it did at every step.

Run it from your TeslaMate folder (the one with `docker-compose.yml`), or point it
there with `--compose-dir`. If you have more than one car, add `--car <id>`.

### Optional: backfill the outside temperature

The car's temperature sensor also goes blank during an outage. You can fill in an
**approximate** outside temperature from historical weather data (free, no API
key) by adding `--weather`:

```bash
python3 run.py --commit --compose-dir /path/to/teslamate --weather
```

This is clearly-labelled ambient air temperature, not real sensor readings.

---

## Is it safe?

It's built to be careful:

- **Read-only by default.** Nothing is written unless you add `--commit`.
- **Backup first.** `run.py --commit` takes a `pg_dump` before the first write.
- **One confirmation**, then it runs unattended (add `--yes` to skip even that).
- **Surgical writes.** It only fills in *missing* data on the trips it recovered.
  Real recorded data and healthy drives are never overwritten.
- **It refuses to do something dumb.** If the battery calibration looks unreliable,
  or a trip would be damaged by closing it too early, it stops and tells you.

If you'd like to rehearse the whole thing risk-free first, restore a copy of your
database into the throwaway test stack in [`docker_test/`](docker_test/) and run
the scripts against that before touching your live data.

---

## Want more control?

`run.py` is just an orchestrator. Each step is a standalone script you can run by
hand if you want to inspect or adjust things between stages:

| Step | Script | What it does |
|------|--------|--------------|
| — | `preview.py` | **Read-only** full dry run — shows the trips it *would* create |
| 1 | `1_diagnose.py` | Report the outage and the stuck drive (read-only) |
| 2 | `2_calibrate.py` | Work out this car's battery range per % charge |
| 3 | `3_segment.py` | Split the stuck drive into the real individual trips |
| 4 | `4_reconstruct.py` | Fill in the missing battery range on each trip |
| 5 | `5_finalize.py` | Close each trip natively (distance, addresses, etc.) |
| 6 | `6_weather.py` | Optional approximate outside temperature |
| — | `selftest.py` | Offline math check, no database needed |

Every step is dry-run by default and prints a `NEXT:` hint for the following one.
Run any of them with `--help` for its options.

---

## How does it actually work?

The interesting parts — how it tells a real outage apart from a sleeping car, why
battery calibration is trickier than "range ÷ percent", how the missing range is
reconstructed, and the one TeslaMate quirk that can silently delete a drive — are
written up in **[HOW_IT_WORKS.md](HOW_IT_WORKS.md)**.

---

## License

[BSD Zero Clause License (0BSD)](LICENSE) — do anything you like with this, no
attribution required. Provided as-is, with no warranty.
