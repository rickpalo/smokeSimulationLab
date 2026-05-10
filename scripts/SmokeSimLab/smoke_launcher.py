"""
smoke_launcher.py
=================
Crash-safe wrapper for a single SmokeSimLab batch job.

Usage (written into run_smoke_batch.bat by export_batch)
---------------------------------------------------------
    python smoke_launcher.py <blender_exe> <job_json>

Behaviour
---------
1. Reads <job_json> for blend_file and render_mode, then launches:
       blender_exe blend_file [--background | --window-geometry ...]
           --factory-startup --python smoke_worker.py -- job_json
   Blender's stderr (C++ startup noise) is suppressed; stdout passes
   through to the batch console window as before.
2. Polls every 2 s for a WerFault.exe process whose command line contains
   "-p <blender_pid>" — the Windows crash-dialog signature.
3. On crash detection:
       * Copies %TEMP%\\blender.crash.txt (if present) to
         <jobs_dir>/<job_stem>_crash_<YYYYMMDD_HHMMSS>.txt
       * Kills WerFault (dismisses the dialog silently)
       * Kills the Blender process
       * Writes <jobs_dir>/<job_stem>.crashed timestamp marker
       * Exits with code 1  (batch then writes job_NNNN.done = "error")
4. If Blender exits normally: exits with Blender's exit code.

No third-party dependencies — stdlib + tasklist.exe (built into Windows).
"""

import datetime
import json
import os
import subprocess
import sys
import time

_POLL_INTERVAL     = 2.0    # seconds between crash-dialog / stale-log checks
_STALE_LOG_TIMEOUT = 1800   # seconds of log inactivity before killing a stuck job
                            # 30 min: generous enough for high-res bakes and long
                            # animation renders, but still catches a frozen process


# ---------------------------------------------------------------------------
# Crash-dialog detection
# ---------------------------------------------------------------------------

def _find_werfault_for_pid(blender_pid):  # blender_pid kept for API compatibility
    """Return a WerFault.exe PID if one is running, or None.

    Uses tasklist /FI rather than wmic or Get-CimInstance. Both of those
    require reading the CommandLine property of another process, which
    silently returns null without elevation on Windows 11. tasklist needs
    no special privileges and works reliably.

    Jobs run sequentially (one Blender at a time), so any WerFault.exe
    present while our child Blender is alive is targeting our Blender.
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WerFault.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith('"WerFault.exe"'):
                parts = line.split('","')
                if len(parts) >= 2:
                    return int(parts[1])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kill_pid(pid, label):
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10)
        print(f"[smoke_launcher] Killed {label} (PID {pid})")
    except Exception as exc:
        print(f"[smoke_launcher] Warning: could not kill {label} PID {pid}: {exc}")


def _write_crashed_marker(jobs_dir, job_stem):
    marker = os.path.join(jobs_dir, job_stem + ".crashed")
    try:
        with open(marker, "w") as mf:
            mf.write(f"crashed {datetime.datetime.now().isoformat()}\n")
    except OSError:
        pass


def _save_crash_log(jobs_dir, job_stem):
    """Append blender.crash.txt to <output_path>/crash_log.txt with a dated header.

    The file survives re-exports because it lives in output_path, not jobs/.
    Each crash appends a timestamped header block so multiple crashes accumulate
    in one place without overwriting each other.
    """
    crash_src   = os.path.join(
        os.environ.get("TEMP", r"C:\Windows\Temp"), "blender.crash.txt"
    )
    output_path = os.path.dirname(jobs_dir)   # jobs_dir = <output_path>/jobs/
    dest        = os.path.join(output_path, "crash_log.txt")
    ts          = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {ts}  {job_stem} ===\n")
            if os.path.exists(crash_src):
                try:
                    with open(crash_src, "r", encoding="utf-8", errors="replace") as cf:
                        fh.write(cf.read())
                except OSError:
                    fh.write("[could not read blender.crash.txt]\n")
            else:
                fh.write("[no blender.crash.txt found in %TEMP%]\n")
        print(f"[smoke_launcher] Crash log appended → {dest}")
    except OSError as exc:
        print(f"[smoke_launcher] Warning: could not write crash log: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print("Usage: python smoke_launcher.py <blender_exe> <job_json>")
        sys.exit(1)

    blender_exe = sys.argv[1]
    job_json    = os.path.abspath(sys.argv[2])

    with open(job_json, encoding="utf-8") as fh:
        job_data = json.load(fh)

    blend_file         = job_data.get("blend_file", "")
    render_mode        = job_data.get("render_mode", "CYCLES")
    output_path        = job_data.get("output_path", "")
    log_path           = job_data.get("log_path", "")
    collect_crash_logs = job_data.get("collect_crash_logs", False)
    collect_debug_log  = job_data.get("collect_debug_log", False)

    _debug_out = os.path.join(output_path, "debug_log.txt") if output_path else ""

    def _dlog(msg):
        """Append one timestamped line to debug_log.txt. No-op when flag is off."""
        ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts}  [launcher/{job_stem}]  {msg}"
        print(line)
        if collect_debug_log and _debug_out:
            try:
                with open(_debug_out, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    # smoke_worker.py is exported to output_path alongside run_smoke_batch.bat
    worker_py = os.path.join(output_path, "smoke_worker.py")

    jobs_dir = os.path.dirname(job_json)
    job_stem = os.path.splitext(os.path.basename(job_json))[0]

    if render_mode == "EEVEE":
        cmd = [blender_exe, blend_file,
               "--window-geometry", "0", "0", "100", "100",
               "--factory-startup",
               "--python", worker_py,
               "--", job_json]
    else:
        cmd = [blender_exe, blend_file,
               "--background", "--factory-startup",
               "--python", worker_py,
               "--", job_json]

    _dlog(f"startup: python={sys.version.split()[0]}  platform={sys.platform}  "
          f"blender_exe={blender_exe!r}  job_json={job_json!r}")
    _dlog(f"cmd: {cmd}")
    print(f"[smoke_launcher] Starting job {job_stem}")

    # Suppress the WerFault crash dialog before spawning Blender.
    # SEM_NOGPFAULTERRORBOX is inherited by child processes: when Blender
    # crashes, Windows skips the interactive dialog and lets the process
    # terminate immediately with a non-zero exit code.
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0002)  # SEM_NOGPFAULTERRORBOX
        print("[smoke_launcher] SEM_NOGPFAULTERRORBOX set — crash dialogs suppressed")
    except Exception as e:
        print(f"[smoke_launcher] Warning: could not set error mode: {e}")

    # stderr=DEVNULL suppresses Blender's C++ startup noise; stdout is
    # inherited so live worker output still appears in the batch window.
    proc        = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
    blender_pid = proc.pid
    print(f"[smoke_launcher] Blender PID {blender_pid}")

    # Stale-log watchdog state.
    # last_log_mtime  — the mtime of the log file at the last poll where it existed.
    # stale_since     — wall-clock time when the mtime first stopped changing;
    #                   None means either the file hasn't appeared yet, or it was
    #                   recently active.
    last_log_mtime = None
    stale_since    = None

    while True:
        if proc.poll() is not None:
            break  # Blender exited (clean or crash)

        # ── Stale-log watchdog ───────────────────────────────────────────────
        # If the job's log file exists but has not been written to for
        # _STALE_LOG_TIMEOUT seconds, assume the process is frozen and kill it.
        # This catches crashes that bypass WerFault (e.g. silent hangs in the
        # Mantaflow solver or the Cycles GPU kernel).
        if log_path:
            try:
                cur_mtime = os.path.getmtime(log_path)
            except OSError:
                cur_mtime = None
            if cur_mtime is not None:
                if cur_mtime != last_log_mtime:
                    last_log_mtime = cur_mtime
                    stale_since    = time.time()   # reset stale clock on any activity
                elif stale_since is not None:
                    idle_secs = time.time() - stale_since
                    if idle_secs >= _STALE_LOG_TIMEOUT:
                        _dlog(f"stale watchdog: idle={int(idle_secs)}s  threshold={_STALE_LOG_TIMEOUT}s")
                        print(f"[smoke_launcher] No log activity for "
                              f"{int(idle_secs)}s — killing stuck job {job_stem}")
                        if collect_crash_logs:
                            _save_crash_log(jobs_dir, job_stem)
                        _kill_pid(blender_pid, "Blender (stale)")
                        proc.wait()
                        _write_crashed_marker(jobs_dir, job_stem)
                        sys.exit(1)

        # ── Belt-and-suspenders WerFault check ──────────────────────────────
        # If SetErrorMode didn't suppress the dialog on this machine, detect
        # and kill WerFault so the batch doesn't hang.
        wer_pid = _find_werfault_for_pid(blender_pid)
        if wer_pid is not None:
            print(f"[smoke_launcher] WerFault PID {wer_pid} detected — killing")
            if collect_crash_logs:
                _save_crash_log(jobs_dir, job_stem)
            _kill_pid(wer_pid, "WerFault")
            _kill_pid(blender_pid, "Blender")
            proc.wait()
            _write_crashed_marker(jobs_dir, job_stem)
            print(f"[smoke_launcher] Job {job_stem} CRASHED")
            sys.exit(1)

        time.sleep(_POLL_INTERVAL)

    exit_code = proc.returncode
    if exit_code != 0:
        # Post-exit WerFault check: the dialog can appear briefly AFTER Blender
        # terminates.  Poll a few times so we dismiss and save the log before
        # writing the crash marker.
        for _attempt in range(3):
            wer_pid = _find_werfault_for_pid(blender_pid)
            if wer_pid is not None:
                print(f"[smoke_launcher] Post-exit WerFault PID {wer_pid} — killing")
                _kill_pid(wer_pid, "WerFault")
                break
            time.sleep(1.0)
        if collect_crash_logs:
            _save_crash_log(jobs_dir, job_stem)
        _write_crashed_marker(jobs_dir, job_stem)
        _dlog(f"exit: CRASHED  exit_code={exit_code}")
        print(f"[smoke_launcher] Job {job_stem} CRASHED (exit {exit_code})")
        sys.exit(1)

    _dlog(f"exit: OK  exit_code=0")
    print(f"[smoke_launcher] Job {job_stem} OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
