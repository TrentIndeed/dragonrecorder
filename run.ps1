# DragonRecorder launcher - no flags, no setup steps.
# Holds the app in the foreground: Ctrl+C (or closing this window) kills the
# whole tree - tray, bridge, webcam windows, ffmpeg, whisper.
# ASCII only: PowerShell 5.1 reads unmarked UTF-8 as ANSI and chokes on it.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# an instance from a previous/detached launch? take it over so THIS window
# owns the process - unless it's mid-recording.
try {
    $status = (Invoke-WebRequest -Uri "http://127.0.0.1:8477/status" -UseBasicParsing -TimeoutSec 2).Content
    if ($status -match "RECORDING|PAUSED") {
        Write-Host "DragonRecorder is recording right now - not touching it."
        Write-Host "Stop the take (Ctrl+Alt+C) and run this again."
        exit 1
    }
    Write-Host "Found a running instance - restarting it under this window..."
    if ($status -match '"pid":\s*(\d+)') {
        & taskkill /PID $Matches[1] /T /F 2>$null | Out-Null
    } else {
        # older instance without /status: kill by command line
        Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*dragonrecorder*" -and $_.Name -match "^python" } | ForEach-Object { & taskkill /PID $_.ProcessId /T /F 2>$null | Out-Null }
    }
    Start-Sleep 1
} catch {
    # /status 404s on older instances (throws in PS) - probe /ping instead
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:8477/ping" -UseBasicParsing -TimeoutSec 2 | Out-Null
        Write-Host "Found an older running instance - restarting it under this window..."
        Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*dragonrecorder*" -and $_.Name -match "^python" } | ForEach-Object { & taskkill /PID $_.ProcessId /T /F 2>$null | Out-Null }
        Start-Sleep 1
    } catch {}
}
# wait until the old bridge is confirmed dead, or the new instance's own
# already-running guard sees the dying socket and quits immediately
for ($i = 0; $i -lt 12; $i++) {
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:8477/ping" -UseBasicParsing -TimeoutSec 1 | Out-Null
        Start-Sleep -Milliseconds 500
    } catch { break }
}

if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Host "First run: creating virtualenv..."
    python -m venv "$root\.venv"
}
& "$root\.venv\Scripts\python.exe" -m pip install -q -r "$root\client\requirements.txt"

# console python attached to this window; output captured for diagnosis
$proc = Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
    -ArgumentList "-m", "dragonrecorder" -WorkingDirectory "$root\client" `
    -NoNewWindow -RedirectStandardError "$root\launcher.log" -PassThru

$dashUrl = ""
try {
    $line = Select-String -Path "$root\.env" -Pattern "^SERVER_URL=" | Select-Object -First 1
    if ($line) { $dashUrl = $line.Line.Substring(11).Trim() }
} catch {}
Write-Host "DragonRecorder is running (Ctrl+C here to quit everything)."
Write-Host "  Record hotkey: Ctrl+Alt+C (opens the panel)"
if ($dashUrl) { Write-Host "  Library:       $dashUrl/dash" }

$startTs = Get-Date
try {
    Wait-Process -Id $proc.Id
    if (((Get-Date) - $startTs).TotalSeconds -lt 15) {
        Write-Host ""
        Write-Host "DragonRecorder exited immediately. Log tail:"
        Get-Content "$root\launcher.log" -Tail 25 -ErrorAction SilentlyContinue
    }
} finally {
    # Ctrl+C, window close, or app exit -> force-kill the whole tree so no
    # ffmpeg/webview child is left behind
    & taskkill /PID $proc.Id /T /F 2>$null | Out-Null
}
