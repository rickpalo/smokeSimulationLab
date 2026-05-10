"""
analyze_estim.py
================
Read estim_log.jsonl produced by SmokeSimLab and report how accurate
each estimation stage was.  Use this to calibrate the rate constants
in __init__.py.

Usage
-----
    python analyze_estim.py <path_to_estim_log.jsonl>
    python analyze_estim.py <directory_containing_estim_log.jsonl>

Output sections
---------------
  BAKE  — implied K per job vs. model constant
  BAKE REAL-TIME  — how good was the first real-time projection?
  RENDER (Cycles / EEVEE)  — same analysis for render
  SETUP — actual setup time vs. _SETUP_SECS_DEFAULT
  STILL — actual still time vs. _STILL_SECS_DEFAULT
  JOB TOTAL — overall elapsed vs. initial total estimate
  PER-JOB SUMMARY TABLE — one line per job
"""

import json
import math
import os
import sys
from collections import defaultdict


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def _mean(v):
    return sum(v) / len(v)

def _stdev(v):
    if len(v) < 2:
        return 0.0
    m = _mean(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))

def _median(v):
    s = sorted(v)
    n = len(s)
    mid = n // 2
    return (s[mid - 1] + s[mid]) / 2 if n % 2 == 0 else s[mid]

def _cv(v):
    m = _mean(v)
    return (_stdev(v) / m * 100) if m else 0.0

def _pct_err(actual, predicted):
    return (actual - predicted) / predicted * 100 if predicted else 0.0

def _fmt(v, width=10, decimals=1):
    return f"{v:{width}.{decimals}f}" if v is not None else f"{'—':>{width}}"

def _fmt_e(v, width=10):
    return f"{v:{width}.4e}" if v is not None else f"{'—':>{width}}"


def _rate_report(label, rates, constant_name):
    print(f"  {label}")
    if not rates:
        print("    (no data)")
        return
    m  = _mean(rates)
    sd = _stdev(rates)
    md = _median(rates)
    cv = _cv(rates)
    print(f"    Samples  : {len(rates)}")
    print(f"    Mean     : {m:.4e}")
    print(f"    Std Dev  : {sd:.4e}")
    print(f"    Median   : {md:.4e}")
    print(f"    CV       : {cv:.1f}%  {'(good fit)' if cv < 20 else '(high variance — mixed hardware/settings?)'}")
    print(f"    Suggested: {constant_name} = {m:.4e}")
    print()


# ---------------------------------------------------------------------------
# Load and group events
# ---------------------------------------------------------------------------

def _load(path):
    records = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"  WARNING: skipping line {lineno}: {exc}", file=sys.stderr)
    return records


def _by_event(records):
    groups = defaultdict(list)
    for r in records:
        groups[r.get("event", "unknown")].append(r)
    return groups


def _jobs_by_name(records):
    """Return dict {job_name: {event_type: record}} — last record wins per type."""
    jobs = defaultdict(dict)
    for r in records:
        name = r.get("job", "")
        etype = r.get("event", "")
        if name and etype:
            jobs[name][etype] = r
    return jobs


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _section_bake(ev, jobs):
    print("BAKE  —  actual vs. model estimate")
    print("  bake_time ≈ _BAKE_RATE_PER_RES3_FRAME × resolution³ × frames")
    print()

    actuals = [r for r in ev.get("bake_actual", [])
               if r.get("source") != "cache_skip" and r.get("implied_rate") is not None]

    implied = [r["implied_rate"] for r in actuals]
    model_rate = actuals[0]["model_rate"] if actuals else None

    _rate_report("implied_rate (s / res³ / frame)", implied, "_BAKE_RATE_PER_RES3_FRAME")

    if model_rate is not None:
        print(f"  Current model rate: {model_rate:.4e}")
        if implied:
            ratio_to_model = _mean(implied) / model_rate
            print(f"  Mean implied / model: {ratio_to_model:.2f}×  "
                  f"({'underestimates — increase constant' if ratio_to_model > 1.0 else 'overestimates — decrease constant'})")
        print()

    # Per-job detail
    if actuals:
        w_job = max(len(r.get("job","")) for r in actuals)
        w_job = max(w_job, 36)
        hdr = (f"  {'Job':<{w_job}} {'Res':>5} {'Frames':>7} "
               f"{'Default':>10} {'Actual':>10} {'Ratio':>7} {'ImpliedRate':>12}")
        sep = f"  {'-'*w_job} {'-'*5} {'-'*7} {'-'*10} {'-'*10} {'-'*7} {'-'*12}"
        print(hdr)
        print(sep)
        for r in actuals:
            job   = r.get("job", "?")[:w_job]
            res   = r.get("resolution", "?")
            frm   = r.get("frames", "?")
            dest  = r.get("default_est")
            act   = r.get("actual_secs")
            rat   = r.get("ratio")
            ir    = r.get("implied_rate")
            print(f"  {job:<{w_job}} {res!s:>5} {frm!s:>7} "
                  f"{_fmt(dest)} {_fmt(act)} "
                  f"{_fmt(rat, 7, 2)} {_fmt_e(ir, 12)}")
        print()

    # Cache-skip jobs
    skipped = [r for r in ev.get("bake_actual", []) if r.get("source") == "cache_skip"]
    if skipped:
        print(f"  Cache-skip jobs (bake time = 0, excluded from rate): {len(skipped)}")
        for r in skipped:
            print(f"    {r.get('job','?')}")
        print()


def _section_bake_rt(ev):
    print("BAKE REAL-TIME PREDICTION ACCURACY")
    print("  How accurate was the first real-time projection (after frame 1)?")
    print()

    rt_events  = {r["job"]: r for r in ev.get("bake_rt", []) if "job" in r}
    act_events = {r["job"]: r for r in ev.get("bake_actual", [])
                  if r.get("source") != "cache_skip" and "job" in r}

    paired = [(rt_events[j], act_events[j])
              for j in rt_events if j in act_events]

    if not paired:
        print("  (no paired bake_rt + bake_actual records yet)")
        print()
        return

    pct_errs = []
    print(f"  {'Job':<36} {'@Frame':>7} {'of':>4} {'RT_proj':>10} {'Actual':>10} {'Err%':>8}")
    print(f"  {'-'*36} {'-'*7} {'-'*4} {'-'*10} {'-'*10} {'-'*8}")
    for rt, act in paired:
        rt_proj = rt.get("est_total_rt_secs")
        actual  = act.get("actual_secs")
        if rt_proj is not None and actual is not None and rt_proj > 0:
            err = _pct_err(actual, rt_proj)
            pct_errs.append(err)
            fb   = rt.get("frames_baked", "?")
            tot  = rt.get("total_frames", "?")
            job  = rt.get("job","?")[:36]
            print(f"  {job:<36} {fb!s:>7} {tot!s:>4} "
                  f"{_fmt(rt_proj)} {_fmt(actual)} {err:>+8.1f}%")

    if pct_errs:
        print()
        print(f"  Mean error : {_mean(pct_errs):+.1f}%  "
              f"(negative = over-estimated, positive = under-estimated)")
        print(f"  Std dev    : {_stdev(pct_errs):.1f}%")
    print()


def _section_render(ev, engine):
    key = f"_RENDER_RATE_{engine}_PER_PIXEL_FRAME"
    print(f"RENDER ({engine})  —  actual vs. model estimate")
    print(f"  render_time ≈ {key} × width × height × frames")
    print()

    actuals = [r for r in ev.get("render_actual", [])
               if (r.get("render_mode") or "").upper() == engine
               and r.get("implied_rate") is not None]

    implied = [r["implied_rate"] for r in actuals]
    model_rate = actuals[0]["model_rate"] if actuals else None

    _rate_report("implied_rate (s / pixel / frame)", implied, key)

    if model_rate is not None:
        print(f"  Current model rate: {model_rate:.4e}")
        if implied:
            ratio_to_model = _mean(implied) / model_rate
            print(f"  Mean implied / model: {ratio_to_model:.2f}×  "
                  f"({'underestimates' if ratio_to_model > 1.0 else 'overestimates'})")
        print()

    if actuals:
        w_job = max(len(r.get("job","")) for r in actuals)
        w_job = max(w_job, 36)
        hdr = (f"  {'Job':<{w_job}} {'Pixels':>9} {'Frames':>7} "
               f"{'Default':>10} {'Actual':>10} {'Ratio':>7}")
        sep = f"  {'-'*w_job} {'-'*9} {'-'*7} {'-'*10} {'-'*10} {'-'*7}"
        print(hdr)
        print(sep)
        for r in actuals:
            job = r.get("job","?")[:w_job]
            px  = r.get("render_px","?")
            frm = r.get("frames","?")
            dest = r.get("default_est")
            act  = r.get("actual_secs")
            rat  = r.get("ratio")
            print(f"  {job:<{w_job}} {px!s:>9} {frm!s:>7} "
                  f"{_fmt(dest)} {_fmt(act)} {_fmt(rat, 7, 2)}")
        print()


def _section_render_rt(ev, engine):
    print(f"RENDER REAL-TIME PREDICTION ACCURACY ({engine})")
    print()

    rt_events  = {r["job"]: r for r in ev.get("render_rt", []) if "job" in r}
    act_events = {r["job"]: r for r in ev.get("render_actual", [])
                  if (r.get("render_mode") or "").upper() == engine and "job" in r}

    paired = [(rt_events[j], act_events[j])
              for j in rt_events if j in act_events]

    if not paired:
        print("  (no paired render_rt + render_actual records yet)")
        print()
        return

    pct_errs = []
    print(f"  {'Job':<36} {'@Frame':>7} {'of':>4} {'RT_proj':>10} {'Actual':>10} {'Err%':>8}")
    print(f"  {'-'*36} {'-'*7} {'-'*4} {'-'*10} {'-'*10} {'-'*8}")
    for rt, act in paired:
        rt_proj = rt.get("est_total_rt_secs")
        actual  = act.get("actual_secs")
        if rt_proj is not None and actual is not None and rt_proj > 0:
            err = _pct_err(actual, rt_proj)
            pct_errs.append(err)
            fb   = rt.get("frames_rendered","?")
            tot  = rt.get("total_frames","?")
            job  = rt.get("job","?")[:36]
            print(f"  {job:<36} {fb!s:>7} {tot!s:>4} "
                  f"{_fmt(rt_proj)} {_fmt(actual)} {err:>+8.1f}%")

    if pct_errs:
        print()
        print(f"  Mean error: {_mean(pct_errs):+.1f}%  Std dev: {_stdev(pct_errs):.1f}%")
    print()


def _section_setup(ev):
    print("SETUP  —  time from job start → bake start (vs. _SETUP_SECS_DEFAULT = 10 s)")
    print()

    records = [r for r in ev.get("bake_start", [])
               if r.get("setup_actual_secs") is not None]

    if not records:
        print("  (no data)")
        print()
        return

    actuals = [r["setup_actual_secs"] for r in records]
    default = records[0].get("setup_est_secs", 10.0)

    print(f"  Samples  : {len(actuals)}")
    print(f"  Mean     : {_mean(actuals):.1f} s")
    print(f"  Std Dev  : {_stdev(actuals):.1f} s")
    print(f"  Median   : {_median(actuals):.1f} s")
    print(f"  Default  : {default:.1f} s")
    mean_ratio = _mean(actuals) / default if default else 0
    print(f"  Mean actual / default: {mean_ratio:.2f}×")
    print()

    print(f"  {'Job':<36} {'Actual':>8} {'Default':>8} {'Ratio':>7}")
    print(f"  {'-'*36} {'-'*8} {'-'*8} {'-'*7}")
    for r in records:
        job = r.get("job","?")[:36]
        act = r.get("setup_actual_secs")
        est = r.get("setup_est_secs", 10.0)
        rat = act / est if est else None
        print(f"  {job:<36} {_fmt(act, 8)} {_fmt(est, 8)} {_fmt(rat, 7, 2)}")
    print()


def _section_still(ev):
    print("STILL  —  actual still render time (vs. _STILL_SECS_DEFAULT = 30 s)")
    print()

    records = ev.get("still_actual", [])

    if not records:
        print("  (no data)")
        print()
        return

    actuals = [r["actual_secs"] for r in records if r.get("actual_secs") is not None]
    default = records[0].get("est_secs", 30.0)

    print(f"  Samples  : {len(actuals)}")
    print(f"  Mean     : {_mean(actuals):.1f} s")
    print(f"  Std Dev  : {_stdev(actuals):.1f} s")
    print(f"  Median   : {_median(actuals):.1f} s")
    print(f"  Default  : {default:.1f} s")
    mean_ratio = _mean(actuals) / default if default else 0
    print(f"  Mean actual / default: {mean_ratio:.2f}×  "
          f"({'Suggested: _STILL_SECS_DEFAULT = ' + str(round(_mean(actuals))) if abs(mean_ratio - 1.0) > 0.25 else 'within 25% — no change needed'})")
    print()

    print(f"  {'Job':<36} {'Actual':>8} {'Default':>8} {'Ratio':>7}")
    print(f"  {'-'*36} {'-'*8} {'-'*8} {'-'*7}")
    for r in records:
        job = r.get("job","?")[:36]
        act = r.get("actual_secs")
        est = r.get("est_secs", 30.0)
        rat = r.get("ratio")
        print(f"  {job:<36} {_fmt(act, 8)} {_fmt(est, 8)} {_fmt(rat, 7, 2)}")
    print()


def _section_job_total(ev):
    print("JOB TOTAL ACCURACY  —  elapsed vs. initial model estimate")
    print()

    records = [r for r in ev.get("job_complete", [])
               if r.get("ratio") is not None and r.get("est_total_0", 0) > 0]

    if not records:
        print("  (no data)")
        print()
        return

    ratios = [r["ratio"] for r in records]
    print(f"  Samples         : {len(ratios)}")
    print(f"  Mean ratio      : {_mean(ratios):.2f}×  (1.0 = perfect)")
    print(f"  Std dev         : {_stdev(ratios):.2f}")
    print(f"  Median          : {_median(ratios):.2f}×")
    print(f"  Min / Max       : {min(ratios):.2f}× / {max(ratios):.2f}×")
    m = _mean(ratios)
    if m > 1.15:
        print(f"  → Model consistently underestimates by {(m-1)*100:.0f}%.")
        print(f"    Increase rate constants or check hardware.")
    elif m < 0.85:
        print(f"  → Model consistently overestimates by {(1-m)*100:.0f}%.")
        print(f"    Decrease rate constants.")
    else:
        print(f"  → Model is within 15% on average. Good.")
    print()


def _section_summary_table(ev, jobs):
    print("PER-JOB SUMMARY")
    print()

    jc_map  = {r["job"]: r for r in ev.get("job_complete", []) if "job" in r}
    js_map  = {r["job"]: r for r in ev.get("job_start",    []) if "job" in r}
    ba_map  = {r["job"]: r for r in ev.get("bake_actual",  []) if "job" in r}
    ra_map  = {r["job"]: r for r in ev.get("render_actual",[]) if "job" in r}
    sa_map  = {r["job"]: r for r in ev.get("still_actual", []) if "job" in r}

    all_jobs = sorted(set(list(jc_map) + list(js_map)))
    if not all_jobs:
        print("  (no job_complete records yet)")
        return

    w = max(len(j) for j in all_jobs)
    w = max(w, 36)
    hdr = (f"  {'Job':<{w}} {'Res':>5} {'Frms':>5} "
           f"{'EstTotal':>9} {'Elapsed':>9} {'Ratio':>6} "
           f"{'BakeAct':>8} {'RndAct':>8} {'StillAct':>9}")
    sep = (f"  {'-'*w} {'-'*5} {'-'*5} "
           f"{'-'*9} {'-'*9} {'-'*6} "
           f"{'-'*8} {'-'*8} {'-'*9}")
    print(hdr)
    print(sep)

    for job in all_jobs:
        js  = js_map.get(job, {})
        jc  = jc_map.get(job, {})
        ba  = ba_map.get(job, {})
        ra  = ra_map.get(job, {})
        sa  = sa_map.get(job, {})

        res       = js.get("resolution", "?")
        frms      = js.get("frames", "?")
        est_total = jc.get("est_total_0") or js.get("est_total")
        elapsed   = jc.get("elapsed_secs")
        ratio     = jc.get("ratio")
        bake_act  = ba.get("actual_secs") if ba.get("source") != "cache_skip" else 0.0
        rnd_act   = ra.get("actual_secs")
        still_act = sa.get("actual_secs")

        ratio_s = f"{ratio:.2f}×" if ratio is not None else "—"
        print(f"  {job[:w]:<{w}} {res!s:>5} {frms!s:>5} "
              f"{_fmt(est_total, 9)} {_fmt(elapsed, 9)} {ratio_s:>6} "
              f"{_fmt(bake_act, 8)} {_fmt(rnd_act, 8)} {_fmt(still_act, 9)}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyze(path):
    if os.path.isdir(path):
        path = os.path.join(path, "estim_log.jsonl")
    if not os.path.exists(path):
        print(f"ERROR: file not found: {path}")
        sys.exit(1)

    records = _load(path)
    if not records:
        print("estim_log.jsonl is empty — run some batch jobs first.")
        return

    ev   = _by_event(records)
    jobs = _jobs_by_name(records)

    # Count batches and jobs
    n_batches = len(ev.get("batch_start", []))
    n_jobs    = len(ev.get("job_start",   []))
    n_done    = len(ev.get("job_complete",[]))

    divider = "=" * 68
    thin    = "-" * 68
    print(divider)
    print("SmokeSimLab Estimation Log Analysis")
    print(f"File     : {path}")
    print(f"Batches  : {n_batches}   Jobs started: {n_jobs}   Jobs completed: {n_done}")
    print(divider)
    print()

    _section_bake(ev, jobs)
    print(thin); print()
    _section_bake_rt(ev)
    print(thin); print()
    _section_render(ev, "CYCLES")
    print(thin); print()
    _section_render_rt(ev, "CYCLES")
    print(thin); print()
    _section_render(ev, "EEVEE")
    print(thin); print()
    _section_render_rt(ev, "EEVEE")
    print(thin); print()
    _section_setup(ev)
    print(thin); print()
    _section_still(ev)
    print(thin); print()
    _section_job_total(ev)
    print(thin); print()
    _section_summary_table(ev, jobs)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("Usage: python analyze_estim.py <path_to_estim_log.jsonl>")
        print("       python analyze_estim.py <output_directory>")
        sys.exit(1)
    analyze(sys.argv[1])
