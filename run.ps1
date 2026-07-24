# DragonRecorder launcher - no flags, no setup steps.
# Creates the venv on first run, keeps deps current, starts the tray app.
# ASCII only: PowerShell 5.1 reads unmarked UTF-8 as ANSI and chokes on it.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# already running? just open its panel and be done
try {
    Invoke-WebRequest -Uri "http://127.0.0.1:8477/open" -Method POST -UseBasicParsing -TimeoutSec 2 | Out-Null
    Write-Host "DragonRecorder is already running - opened its panel (top-right)."
    exit 0
} catch {}

if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Host "First run: creating virtualenv..."
    python -m venv "$root\.venv"
}
& "$root\.venv\Scripts\python.exe" -m pip install -q -r "$root\client\requirements.txt"

# pythonw = no console window; the app lives in the tray
Start-Process -FilePath "$root\.venv\Scripts\pythonw.exe" -ArgumentList "-m", "dragonrecorder" -WorkingDirectory "$root\client"
Write-Host "DragonRecorder is running - look for the tray icon. The record hotkey opens the panel."
