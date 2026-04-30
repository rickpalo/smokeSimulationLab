@echo off
cd /d "%~dp0"
echo SmokeSimLab test suite
echo ========================
python -m pytest tests\ -v
echo.
if errorlevel 1 (
    echo TESTS FAILED
) else (
    echo All tests passed.
)
pause
