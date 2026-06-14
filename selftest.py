#!/usr/bin/env python3
"""
selftest.py — offline unit tests for the pure reconstruction math in common.py.
No database and no psycopg2 needed. Run anywhere:  python3 selftest.py
"""
import datetime as dt
import common as c


def mkpos(rows, t0=dt.datetime(2026, 6, 13, 20, 0, 0)):
    """rows: list of (seconds_offset, power_kW_int, soc_int)."""
    return [dict(id=i, date=t0 + dt.timedelta(seconds=s), power=p, soc=soc)
            for i, (s, p, soc) in enumerate(rows)]


def test_linregress():
    xs = [20, 30, 40, 55, 70, 80]
    ys = [4.0 * x + 2 for x in xs]
    slope, intercept, r2, n = c.linregress(xs, ys)
    assert abs(slope - 4.0) < 1e-9 and abs(intercept - 2.0) < 1e-9 and abs(r2 - 1) < 1e-12
    print(f"  linregress: slope={slope:.5f} intercept={intercept:.4f} r2={r2:.6f}  OK")


def test_segment():
    pos = mkpos([(0, 10, 80), (60, 10, 80), (120, 10, 79), (900, 10, 78), (960, 10, 78)])
    segs = c.segment_by_gap(pos, 300)
    assert [len(s) for s in segs] == [3, 2]
    print(f"  segment_by_gap: sizes={[len(s) for s in segs]}  OK")


def test_reconstruct_discharge():
    rows, t, cur = [], 0, 80
    for _ in range(4):
        for _ in range(5):
            rows.append((t, 36, cur)); t += 60
        cur -= 1
    pos = mkpos(rows)
    true, nanch, kwh = c.reconstruct_soc(pos, 0.55)
    in_bucket = all(pos[i]['soc'] - 1e-9 <= true[i] <= pos[i]['soc'] + 1 + 1e-9
                    for i in range(len(pos)))
    monotonic = all(true[i] >= true[i + 1] - 1e-6 for i in range(len(true) - 1))
    assert in_bucket and monotonic and nanch >= 3 and 0.2 < kwh < 5.0
    print(f"  reconstruct (discharge): anchors={nanch} kwh/%={kwh:.3f} "
          f"soc {true[0]:.2f}->{true[-1]:.2f}  OK")


def test_no_inversion():
    pos = mkpos([(0, 40, 80), (60, 40, 79), (120, -200, 79), (3000, 40, 78), (3060, 40, 77)])
    true, _, _ = c.reconstruct_soc(pos, 0.55)
    assert all(pos[i]['soc'] - 1e-9 <= true[i] <= pos[i]['soc'] + 1 + 1e-9
               for i in range(len(pos)))
    print(f"  reconstruct (regen+gap): clamped, no inversion  OK")


def test_no_transition():
    pos = mkpos([(0, 5, 65), (60, 5, 65), (120, 5, 65)])
    true, nanch, kwh = c.reconstruct_soc(pos, 0.55)
    assert nanch == 0 and all(abs(x - 65.5) < 1e-9 for x in true) and kwh == 0.55
    print(f"  reconstruct (flat SOC): midpoint 65.5, fallback kwh  OK")


if __name__ == "__main__":
    print("Running offline self-tests (no DB)...")
    test_linregress()
    test_segment()
    test_reconstruct_discharge()
    test_no_inversion()
    test_no_transition()
    print("ALL PASSED")
