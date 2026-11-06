@echo off
REM Simulates users uploading files to the real Nextcloud (Docker stack must be running).
REM Uploads about 15 MB every 5 seconds and stops by itself after 150 MB.
cd /d "%~dp0"
python -c "import httpx" 2>nul || python -m pip install -q httpx
start "" http://localhost:8000
echo Uploading test files to Nextcloud... watch the dashboard at http://localhost:8000
echo (click "Collect now" to refresh immediately; Ctrl+C here to stop early)
echo.
python scripts/generate_load.py --scenario steady --interval 5 --max-gb 0.15
echo.
echo Done. Predictive alerts resolve by themselves about 15 minutes after uploads stop.
pause
