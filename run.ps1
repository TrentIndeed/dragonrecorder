# DragonRecorder launcher - no flags, no setup steps.
# Holds the app in the foreground: Ctrl+C (or closing this window) kills the
# whole tree - tray, bridge, webcam windows, ffmpeg, whisper.
# ASCII only: PowerShell 5.1 reads unmarked UTF-8 as ANSI and chokes on it.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# already running from a previous launch? just open its panel and exit
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

# python.exe attached to this console (-NoNewWindow) so Ctrl+C reaches it
$proc = Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
    -ArgumentList "-m", "dragonrecorder" -WorkingDirectory "$root\client" `
    -NoNewWindow -PassThru

Write-Host "DragonRecorder is running (Ctrl+C here to quit everything)."
Write-Host "The record hotkey (Ctrl+Alt+C) opens the panel."
try {
    Wait-Process -Id $proc.Id
} finally {
    # Ctrl+C, window close, or app exit -> force-kill the whole tree so no
    # ffmpeg/webview child is left behind
    & taskkill /PID $proc.Id /T /F 2>$null | Out-Null
}
