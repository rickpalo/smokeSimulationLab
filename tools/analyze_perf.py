"""
analyze_perf.py
===============
Read perf_log.json produced by SmokeSimLab and report scaling statistics.

Usage
-----
    python analyze_perf.py <path_to_perf_log.json>

Output
------
For each metric the script prints:
  - Number of data points
  - Mean and standard deviation
  - Coefficient of variation (std/mean) — low CV means the linear model fits well
  - Suggested constant to paste into __init__.py

Metrics reported
----------------
  bake_secs_per_res3_frame
      Tests whether  bake_time ≈ K × resolution³ × frames.
      A stable K (low CV) confirms the linear model; update
      _BAKE_RATE_PER_RES3_FRAME in __init__.py with the mean.

  render_secs_per_pixel_frame  (Cycles and EEVEE reported separately)
      Tests whether  render_time ≈ K × width × height × frames.
      A stable K confirms the linear model; update
      _RENDER_RATE_CYCLES_PER_PIXEL_FRAME / _RENDER_RATE_EEVEE_PER_PIXEL_FRAME.
"""

import json
import math
import sys
import os


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def _mean(values):
    return sum(values) / len(values)


def _stdev(values):
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    variance = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return (s[mid - 1] + s[mid]) / 2 if n % 2 == 0 else s[mid]


def _report(label, values, constant_name):
    print(f"  {label}")
    if not values:
        print("    (no data)")
        return
    m  = _mean(values)
    sd = _stdev(values)
    md = _median(values)
    cv = (sd / m * 100) if m != 0 else 0.0
    print(f"    Samples  : {len(values)}")
    print(f"    Mean     : {m:.4e}")
    print(f"    Std Dev  : {sd:.4e}")
    print(f"    Median   : {md:.4e}")
    print(f"    CV       : {cv:.1f}%  {'(good fit)' if cv < 20 else '(high variance — mixed hardware/settings?)'}")
    print(f"    → Paste into __init__.py:  {constant_name} = {m:.4e}")
    print()


# ---------------------------------------------------------------------------
# Engine normalisation
# ---------------------------------------------------------------------------

_CYCLES_NAMES = {"CYCLES"}
_EEVEE_NAMES  = {"EEVEE", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}


def _engine_group(engine_str):
    e = (engine_str or "").upper()
    if e in _CYCLES_NAMES:
        return "CYCLES"
    if e in _EEVEE_NAMES:
        return "EEVEE"
    return "OTHER"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyze(perf_path):
    if not os.path.exists(perf_path):
        print(f"ERROR: file not found: {perf_path}")
        sys.exit(1)

    with open(perf_path, encoding="utf-8") as fh:
        records = json.load(fh)

    if not records:
        print("perf_log.json is empty — run some batch jobs first.")
        return

    print("=" * 64)
    print(f"SmokeSimLab Performance Analysis")
    print(f"File    : {perf_path}")
    print(f"Records : {len(records)}")
    print("=" * 64)
    print()

    # ------------------------------------------------------------------
    # Bake scaling
    # ------------------------------------------------------------------
    bake_rates = [
        r["bake_secs_per_res3_frame"]
        for r in records
        if not r.get("bake_skipped")
        and r.get("bake_secs_per_res3_frame") is not None
    ]

    print("BAKE  —  bake_time ≈ K × resolution³ × frames")
    _report("bake_secs_per_res3_frame", bake_rates, "_BAKE_RATE_PER_RES3_FRAME")

    # Sanity table: actual vs. predicted for each bake record
    bake_detail = [
        r for r in records
        if not r.get("bake_skipped")
        and r.get("bake_secs_per_res3_frame") is not None
        and r.get("bake_seconds") is not None
    ]
    if bake_detail and bake_rates:
        k = _mean(bake_rates)
        print(f"  {'Job':<40} {'Actual':>10} {'Predicted':>10} {'Error%':>8}")
        print(f"  {'-'*40} {'-'*10} {'-'*10} {'-'*8}")
        for r in bake_detail:
            res3     = r["resolution"] ** 3
            frames   = r["frame_end"]
            actual   = r["bake_seconds"]
            pred     = k * res3 * frames
            err_pct  = (actual - pred) / pred * 100 if pred else 0
            name     = r.get("job_name", "?")[:40]
            print(f"  {name:<40} {actual:>10.1f} {pred:>10.1f} {err_pct:>+8.1f}%")
        print()

    # ------------------------------------------------------------------
    # Render scaling — Cycles
    # ------------------------------------------------------------------
    cycles_rates = [
        r["render_secs_per_pixel_frame"]
        for r in records
        if _engine_group(r.get("render_engine")) == "CYCLES"
        and r.get("render_secs_per_pixel_frame") is not None
        and r.get("frames_rendered", 0) > 0
    ]

    print("RENDER (Cycles)  —  render_time ≈ K × width × height × frames")
    _report("render_secs_per_pixel_frame", cycles_rates,
            "_RENDER_RATE_CYCLES_PER_PIXEL_FRAME")

    cycles_detail = [
        r for r in records
        if _engine_group(r.get("render_engine")) == "CYCLES"
        and r.get("render_secs_per_pixel_frame") is not None
        and r.get("render_seconds") is not None
        and r.get("frames_rendered", 0) > 0
    ]
    if cycles_detail and cycles_rates:
        k = _mean(cycles_rates)
        print(f"  {'Job':<40} {'Actual':>10} {'Predicted':>10} {'Error%':>8}")
        print(f"  {'-'*40} {'-'*10} {'-'*10} {'-'*8}")
        for r in cycles_detail:
            pixels   = r.get("render_width", 0) * r.get("render_height", 0)
            frames   = r.get("frames_rendered", 0)
            actual   = r["render_seconds"]
            pred     = k * pixels * frames
            err_pct  = (actual - pred) / pred * 100 if pred else 0
            name     = r.get("job_name", "?")[:40]
            print(f"  {name:<40} {actual:>10.1f} {pred:>10.1f} {err_pct:>+8.1f}%")
        print()

    # ------------------------------------------------------------------
    # Render scaling — EEVEE
    # ------------------------------------------------------------------
    eevee_rates = [
        r["render_secs_per_pixel_frame"]
        for r in records
        if _engine_group(r.get("render_engine")) == "EEVEE"
        and r.get("render_secs_per_pixel_frame") is not None
        and r.get("frames_rendered", 0) > 0
    ]

    print("RENDER (EEVEE)  —  render_time ≈ K × width × height × frames")
    _report("render_secs_per_pixel_frame", eevee_rates,
            "_RENDER_RATE_EEVEE_PER_PIXEL_FRAME")

    eevee_detail = [
        r for r in records
        if _engine_group(r.get("render_engine")) == "EEVEE"
        and r.get("render_secs_per_pixel_frame") is not None
        and r.get("render_seconds") is not None
        and r.get("frames_rendered", 0) > 0
    ]
    if eevee_detail and eevee_rates:
        k = _mean(eevee_rates)
        print(f"  {'Job':<40} {'Actual':>10} {'Predicted':>10} {'Error%':>8}")
        print(f"  {'-'*40} {'-'*10} {'-'*10} {'-'*8}")
        for r in eevee_detail:
            pixels   = r.get("render_width", 0) * r.get("render_height", 0)
            frames   = r.get("frames_rendered", 0)
            actual   = r["render_seconds"]
            pred     = k * pixels * frames
            err_pct  = (actual - pred) / pred * 100 if pred else 0
            name     = r.get("job_name", "?")[:40]
            print(f"  {name:<40} {actual:>10.1f} {pred:>10.1f} {err_pct:>+8.1f}%")
        print()

    # ------------------------------------------------------------------
    # Raw record summary
    # ------------------------------------------------------------------
    print("-" * 64)
    print("Raw records (most recent first):")
    print()
    header = (f"  {'Timestamp':<20} {'Resolution':>10} {'Bake s/f':>10} "
              f"{'Engine':<8} {'Res WxH':<12} {'Rnd s/f':>10}")
    print(header)
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*8} {'-'*12} {'-'*10}")
    for r in reversed(records):
        ts   = r.get("timestamp", "")[:20]
        res  = r.get("resolution", "?")
        bspf = r.get("bake_secs_per_frame")
        bspf_s = f"{bspf:.4f}" if bspf is not None else "skipped"
        eng  = r.get("render_engine", "?")
        rw   = r.get("render_width", 0)
        rh   = r.get("render_height", 0)
        dims = f"{rw}×{rh}" if rw and rh else "?"
        rspf = r.get("render_secs_per_frame")
        rspf_s = f"{rspf:.4f}" if rspf is not None else "skipped"
        print(f"  {ts:<20} {res!s:>10} {bspf_s:>10} {eng:<8} {dims:<12} {rspf_s:>10}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("Usage: python analyze_perf.py <path_to_perf_log.json>")
        sys.exit(1)
    analyze(sys.argv[1])
