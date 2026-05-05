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
import shutil
import subprocess
import sys
import time

_POLL_INTERVAL = 2.0   # seconds between crash-dialog checks


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
    """Copy blender.crash.txt from %TEMP% to the jobs directory."""
    crash_src = os.path.join(
        os.environ.get("TEMP", r"C:\Windows\Temp"), "blender.crash.txt"
    )
    if not os.path.exists(crash_src):
        print("[smoke_launcher] No blender.crash.txt found in %TEMP%")
        return
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(jobs_dir, f"{job_stem}_crash_{ts}.txt")
    try:
        shutil.copy2(crash_src, dest)
        print(f"[smoke_launcher] Crash log saved → {dest}")
    except OSError as exc:
        print(f"[smoke_launcher] Warning: could not copy crash log: {exc}")


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

    blend_file  = job_data.get("blend_file", "")
    render_mode = job_data.get("render_mode", "CYCLES")
    output_path = job_data.get("output_path", "")

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

    while True:
        if proc.poll() is not None:
            break  # Blender exited (clean or crash)

        # Belt-and-suspenders: if SetErrorMode didn't suppress the dialog on
        # this machine, detect and kill WerFault so the batch doesn't hang.
        wer_pid = _find_werfault_for_pid(blender_pid)
        if wer_pid is not None:
            print(f"[smoke_launcher] WerFault PID {wer_pid} detected — killing")
            _save_crash_log(jobs_dir, job_stem)
            _kill_pid(wer_pid, "WerFault")
            _kill_pid(blender_pid, "Blender")
            proc.wait()
            _write_crashed_marker(jobs_dir, job_stem)
            print(f"[smoke_launcher] Job {job_stem} CRASHED — crash log saved to jobs/")
            sys.exit(1)

        time.sleep(_POLL_INTERVAL)

    exit_code = proc.returncode
    if exit_code != 0:
        _save_crash_log(jobs_dir, job_stem)
        _write_crashed_marker(jobs_dir, job_stem)
        print(f"[smoke_launcher] Job {job_stem} CRASHED (exit {exit_code}) — crash log saved to jobs/")
        sys.exit(1)

    print(f"[smoke_launcher] Job {job_stem} OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
