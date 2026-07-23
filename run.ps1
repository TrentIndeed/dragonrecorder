# DragonRecorder launcher — no flags, no setup steps.
# Creates the venv on first run, keeps deps current, starts the tray app.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Host "First run: creating virtualenv..."
    python -m venv "$root\.venv"
}
& "$root\.venv\Scripts\python.exe" -m pip install -q -r "$root\client\requirements.txt"

Set-Location "$root\client"
# pythonw = no console window; the app lives in the tray
Start-Process -FilePath "$root\.venv\Scripts\pythonw.exe" -ArgumentList "-m", "dragonrecorder" -WorkingDirectory "$root\client"
Write-Host "DragonRecorder is running — look for the tray icon. Hotkey opens the panel."
