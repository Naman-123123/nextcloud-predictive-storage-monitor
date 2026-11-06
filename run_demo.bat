@echo off
REM Starts the monitoring dashboard in DEMO mode (simulated Nextcloud, no Docker needed).
cd /d "%~dp0monitor"
echo Installing Python dependencies...
python -m pip install -q -r requirements.txt
if not exist "%LOCALAPPDATA%\nextcloud-monitor" mkdir "%LOCALAPPDATA%\nextcloud-monitor"
set MONITOR_MODE=demo
set DATABASE_URL=sqlite:///%LOCALAPPDATA:\=/%/nextcloud-monitor/monitor.db
set COLLECT_INTERVAL_SECONDS=30
start "" cmd /c "timeout /t 5 >nul & start http://localhost:8000"
echo Dashboard: http://localhost:8000   (Ctrl+C to stop)
python -m uvicorn app.main:app --port 8000
