@echo off
REM ===========================================================================
REM run_bg_probe_v2.bat — TODO-35 probe (user's refined 5-step proposal:
REM stash → assign-on-empty → save → MOVE-BACK → reload + bake).
REM
REM Three sequential --background --factory-startup Blender invocations:
REM   STEP A (setup)   — bake 100 frames fresh into <CACHE>.
REM   STEP B (prepare) — move <CACHE> → <PRESERVE>; assign cache_directory =
REM                       <TARGET> (empty, wipe-safe); save <TMP_BLEND>; then
REM                       move <PRESERVE> → <TARGET> (step 3.5).
REM   STEP C (test)    — open <TMP_BLEND>; bake to 200; report mtime
REM                       preservation; print VERDICT.
REM
REM Look for "VERDICT:" lines (final one is in STEP C).
REM ===========================================================================
setlocal

set "BLENDER=C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
set "BLEND=E:\BlenderSync\SynologyDrive\ImageOfTheMonth\2018\November - Canaletto\SmokeSimulatorForPiazzoSanMarco.blend"
set "SCRIPT=%~dp0bg_resume_probe_v2.py"
set "CACHE=E:\BlenderSync\SynologyDrive\ImageOfTheMonth\2018\November - Canaletto\smokeTesting\bg_probe_v2_cache"
set "PRESERVE=E:\BlenderSync\SynologyDrive\ImageOfTheMonth\2018\November - Canaletto\smokeTesting\bg_probe_v2_preserve"
set "TARGET=E:\BlenderSync\SynologyDrive\ImageOfTheMonth\2018\November - Canaletto\smokeTesting\bg_probe_v2_target"
set "TMP_BLEND=E:\BlenderSync\SynologyDrive\ImageOfTheMonth\2018\November - Canaletto\smokeTesting\bg_probe_v2.blend"
set "DOMAIN=Smoke Domain"

echo === Cleaning previous probe state ===
if exist "%CACHE%"     rmdir /s /q "%CACHE%"
if exist "%PRESERVE%"  rmdir /s /q "%PRESERVE%"
if exist "%TARGET%"    rmdir /s /q "%TARGET%"
if exist "%TMP_BLEND%" del   /q    "%TMP_BLEND%"

echo.
echo ============ STEP A: bake 100 frames into the test cache ============
"%BLENDER%" --background "%BLEND%" --factory-startup --python "%SCRIPT%" -- setup "%CACHE%" 100 "%DOMAIN%"
if errorlevel 1 (echo STEP A FAILED & pause & exit /b 1)

echo.
echo ============ STEP B: prep — stash, assign-on-empty, save, MOVE-BACK ============
"%BLENDER%" --background "%BLEND%" --factory-startup --python "%SCRIPT%" -- prepare "%CACHE%" "%PRESERVE%" "%TARGET%" "%TMP_BLEND%" "%DOMAIN%"
if errorlevel 1 (echo STEP B FAILED & pause & exit /b 1)

echo.
echo ============ STEP C: open tmp .blend + bake to 200 + report ============
"%BLENDER%" --background "%TMP_BLEND%" --factory-startup --python "%SCRIPT%" -- test "%TARGET%" 200
if errorlevel 1 (echo STEP C FAILED & pause & exit /b 1)

echo.
echo ===========================================================================
echo Look for VERDICT lines (final in STEP C):
echo   RESUMED         = load-time scan detected existing cache -> closes BUG-010
echo   REBAKED-FROM-1  = scan didn't pick up the files
echo   WIPED           = target empty by test time (something destroyed them)
echo   NO PRIOR FRAMES = nothing in target by test time (check STEP B output)
echo ===========================================================================
pause
