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
   Blender's stderr is captured to blender_stderr.txt (one timestamped
   header per job); stdout passes through to the batch console window.
2. Assigns Blender to a Windows Job Object with
   JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION so crash dialogs are
   suppressed even if Blender resets the process error mode internally.
3. Polls every 0.5 s for a WerFault.exe or WerFaultSecure.exe process
   (belt-and-suspenders if the Job Object approach isn't available).
4. Stale-log watchdog: if the job's log file hasn't been written for
   _STALE_LOG_TIMEOUT seconds the job is treated as frozen and killed.
5. On crash detection or non-zero exit:
       * Appends blender.crash.txt (if present) to crash_log.txt
       * Writes <jobs_dir>/<job_stem>.crashed timestamp marker
       * Exits with code 1  (batch then writes job_NNNN.done = "error")
6. If Blender exits normally: exits with Blender's exit code.

No third-party dependencies — stdlib + tasklist.exe (built into Windows).
"""

LAUNCHER_VERSION = "0.2.12"

import atexit
import ctypes
import datetime
import json
import os
import subprocess
import sys
import time

_POLL_INTERVAL          = 0.5   # seconds between crash-dialog / stale-log checks
_STALE_LOG_TIMEOUT      = 1800  # seconds of log inactivity before killing a stuck job
_POST_EXIT_WERFAULT_SECS = 30   # seconds to keep checking for WerFault after exit


# ---------------------------------------------------------------------------
# Windows Job Object — suppress crash dialogs at the OS level
# ---------------------------------------------------------------------------
# JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION is the most reliable method:
# the child process cannot override the Job Object limit even if it calls
# SetErrorMode() internally (which Blender does during startup).  Crash
# dialogs (WerFault / WerFaultSecure) are never spawned.

_JOB_LIMIT_DIE_ON_EXCEPTION         = 0x00000400
_JOB_OBJECT_EXTENDED_LIMIT_INFO     = 9
_PROCESS_ALL_ACCESS                 = 0x1F0FFF


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount",  ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount",   ctypes.c_uint64),
        ("WriteTransferCount",  ctypes.c_uint64),
        ("OtherTransferCount",  ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit",     ctypes.c_int64),
        ("LimitFlags",              ctypes.c_uint32),
        ("MinimumWorkingSetSize",   ctypes.c_size_t),
        ("MaximumWorkingSetSize",   ctypes.c_size_t),
        ("ActiveProcessLimit",      ctypes.c_uint32),
        ("Affinity",                ctypes.c_size_t),
        ("PriorityClass",           ctypes.c_uint32),
        ("SchedulingClass",         ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo",                _IO_COUNTERS),
        ("ProcessMemoryLimit",    ctypes.c_size_t),
        ("JobMemoryLimit",        ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed",     ctypes.c_size_t),
    ]


def _create_crash_suppression_job():
    """Create a Job Object that kills crash dialogs for all member processes.

    Returns the job handle on success, None on any failure.  Failures are
    non-fatal — the launcher falls back to WerFault polling.
    """
    try:
        k32 = ctypes.windll.kernel32
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_LIMIT_DIE_ON_EXCEPTION
        ok = k32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFO,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            k32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _assign_to_job(job_handle, pid):
    """Assign the process with the given PID to job_handle.

    Returns True on success, False otherwise.
    """
    try:
        k32 = ctypes.windll.kernel32
        proc = k32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
        if not proc:
            return False
        ok = k32.AssignProcessToJobObject(job_handle, proc)
        k32.CloseHandle(proc)
        return bool(ok)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Crash-dialog detection (belt-and-suspenders when Job Object unavailable)
# ---------------------------------------------------------------------------

def _find_werfault_for_pid(blender_pid):  # blender_pid kept for API compatibility
    """Return a WerFault(Secure).exe PID if one is running, or None.

    Uses tasklist /FI rather than wmic or Get-CimInstance.  Jobs run
    sequentially (one Blender at a time), so any WerFault present while
    our child Blender is alive is targeting our Blender.
    """
    for image in ("WerFault.exe", "WerFaultSecure.exe"):
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.lower().startswith(f'"{image.lower()}"'):
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
    """Append blender.crash.txt to <output_path>/crash_log.txt with a dated header."""
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

    blend_file        = job_data.get("blend_file", "")
    render_mode       = job_data.get("render_mode", "CYCLES")
    output_path       = job_data.get("output_path", "")
    log_path          = job_data.get("log_path", "")
    collect_debug_log = job_data.get("collect_debug_log", False)

    _debug_out = os.path.join(output_path, "debug_log.txt") if output_path else ""

    jobs_dir = os.path.dirname(job_json)
    job_stem = os.path.splitext(os.path.basename(job_json))[0]

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

    # Create Job Object with JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION.
    # This is more reliable than SetErrorMode because Blender cannot override
    # a Job Object limit — the OS kills crash dialogs before they appear.
    _job_handle = _create_crash_suppression_job()
    if _job_handle:
        print("[smoke_launcher] Job Object created — crash dialogs suppressed")
    else:
        # Fallback: SetErrorMode, inherited by child but Blender may reset it.
        try:
            ctypes.windll.kernel32.SetErrorMode(0x0002)  # SEM_NOGPFAULTERRORBOX
            print("[smoke_launcher] SEM_NOGPFAULTERRORBOX set (Job Object unavailable)")
        except Exception as e:
            print(f"[smoke_launcher] Warning: could not set error mode: {e}")

    # Redirect Blender's stderr to blender_stderr.txt (append, one header per job).
    # Python errors in the worker script (syntax errors, import failures) go to
    # stderr; without capture they are invisible.
    _stderr_path = os.path.join(output_path, "blender_stderr.txt") if output_path else None
    _stderr_fh   = None
    if _stderr_path:
        try:
            _stderr_fh = open(_stderr_path, "a", encoding="utf-8", errors="replace")
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _stderr_fh.write(f"\n=== {ts}  {job_stem} ===\n")
            _stderr_fh.flush()
            atexit.register(_stderr_fh.close)
        except OSError as exc:
            _dlog(f"warning: could not open blender_stderr.txt: {exc}")
            _stderr_fh = None

    # Record file position after the per-job header so we can read only this
    # job's stderr when checking for Python tracebacks after exit.
    _stderr_start_pos = _stderr_fh.tell() if _stderr_fh is not None else 0

    proc        = subprocess.Popen(cmd, stderr=_stderr_fh if _stderr_fh is not None else subprocess.DEVNULL)
    blender_pid = proc.pid
    print(f"[smoke_launcher] Blender PID {blender_pid}")

    # Assign Blender to the Job Object now that we have its PID.
    if _job_handle:
        if _assign_to_job(_job_handle, blender_pid):
            print(f"[smoke_launcher] Blender PID {blender_pid} assigned to crash-suppression job")
        else:
            print("[smoke_launcher] Warning: could not assign Blender to Job Object")

    # Stale-log watchdog state.
    last_log_mtime = None
    stale_since    = None

    while True:
        if proc.poll() is not None:
            # Check for WerFault in the instant after Blender exits — the
            # dialog can appear briefly after the process terminates.
            wer_pid = _find_werfault_for_pid(blender_pid)
            if wer_pid is not None:
                print(f"[smoke_launcher] WerFault PID {wer_pid} detected at exit — killing")
                _kill_pid(wer_pid, "WerFault")
            break

        # ── Stale-log watchdog ───────────────────────────────────────────────
        if log_path:
            try:
                cur_mtime = os.path.getmtime(log_path)
            except OSError:
                cur_mtime = None
            if cur_mtime is not None:
                if cur_mtime != last_log_mtime:
                    last_log_mtime = cur_mtime
                    stale_since    = time.time()
                elif stale_since is not None:
                    idle_secs = time.time() - stale_since
                    if idle_secs >= _STALE_LOG_TIMEOUT:
                        _dlog(f"stale watchdog: idle={int(idle_secs)}s  threshold={_STALE_LOG_TIMEOUT}s")
                        print(f"[smoke_launcher] No log activity for "
                              f"{int(idle_secs)}s — killing stuck job {job_stem}")
                        _save_crash_log(jobs_dir, job_stem)
                        _kill_pid(blender_pid, "Blender (stale)")
                        proc.wait()
                        _write_crashed_marker(jobs_dir, job_stem)
                        sys.exit(1)

        # ── Belt-and-suspenders WerFault check ──────────────────────────────
        wer_pid = _find_werfault_for_pid(blender_pid)
        if wer_pid is not None:
            print(f"[smoke_launcher] WerFault PID {wer_pid} detected — killing")
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
        # Poll for WerFault for up to _POST_EXIT_WERFAULT_SECS.
        _deadline = time.time() + _POST_EXIT_WERFAULT_SECS
        while time.time() < _deadline:
            wer_pid = _find_werfault_for_pid(blender_pid)
            if wer_pid is not None:
                print(f"[smoke_launcher] Post-exit WerFault PID {wer_pid} — killing")
                _kill_pid(wer_pid, "WerFault")
                break
            time.sleep(1.0)
        _save_crash_log(jobs_dir, job_stem)
        _write_crashed_marker(jobs_dir, job_stem)
        _dlog(f"exit: CRASHED  exit_code={exit_code}")
        print(f"[smoke_launcher] Job {job_stem} CRASHED (exit {exit_code})")
        sys.exit(1)

    # Exit code 0: verify the worker wrote its completion sentinel.
    # A missing sentinel means the worker never reached the end of its script —
    # the job is a silent failure (Python exception swallowed by Blender, or a
    # crash that somehow produced exit code 0).
    worker_done = os.path.join(jobs_dir, job_stem + ".worker_done")
    if not os.path.exists(worker_done):
        # Check stderr for a Python traceback to give a more informative reason.
        _crash_reason = "worker_done sentinel missing"
        if _stderr_path and os.path.exists(_stderr_path):
            try:
                with open(_stderr_path, "r", encoding="utf-8", errors="replace") as _sf:
                    _sf.seek(_stderr_start_pos)
                    _this_stderr = _sf.read()
                if "Traceback (most recent call last)" in _this_stderr:
                    _crash_reason = "Python traceback in stderr (worker_done missing)"
                    _dlog(f"blender_stderr contains Python traceback for this job")
            except OSError:
                pass
        _save_crash_log(jobs_dir, job_stem)
        _write_crashed_marker(jobs_dir, job_stem)
        _dlog(f"exit: CRASHED (exit_code=0)  reason={_crash_reason}")
        print(f"[smoke_launcher] Job {job_stem} CRASHED (exit 0 — {_crash_reason})")
        sys.exit(1)

    _dlog(f"exit: OK  exit_code=0")
    print(f"[smoke_launcher] Job {job_stem} OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
