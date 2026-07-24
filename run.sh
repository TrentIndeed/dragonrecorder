#!/usr/bin/env bash
# DragonRecorder launcher - no flags. Git Bash flavor of run.ps1.
# Holds the app in the foreground: Ctrl+C (or closing this terminal) kills
# the whole tree - tray, bridge, webcam windows, ffmpeg, whisper.
set -e
root="$(cd "$(dirname "$0")" && pwd)"

# an instance from a previous/detached launch? take it over so THIS terminal
# owns the process - unless it's mid-recording.
status="$(curl -s -m 2 http://127.0.0.1:8477/status 2>/dev/null || true)"
if [ -n "$status" ]; then
    case "$status" in
        *RECORDING*|*PAUSED*)
            echo "DragonRecorder is recording right now - not touching it."
            echo "Stop the take (Ctrl+Alt+C) and run this again."
            exit 1;;
    esac
    oldpid="$(printf '%s' "$status" | sed -n 's/.*"pid": *\([0-9]*\).*/\1/p')"
    echo "Found a running instance - restarting it under this terminal..."
    if [ -n "$oldpid" ]; then
        taskkill //PID "$oldpid" //T //F >/dev/null 2>&1 || true
    else
        # older instance without /status: kill by command line
        powershell -NoProfile -WindowStyle Hidden -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*dragonrecorder*' -and \$_.Name -match '^python' } | ForEach-Object { & taskkill /PID \$_.ProcessId /T /F }" >/dev/null 2>&1 || true
    fi
    sleep 1
fi

if [ ! -f "$root/.venv/Scripts/python.exe" ]; then
    echo "First run: creating virtualenv..."
    python -m venv "$root/.venv"
fi
"$root/.venv/Scripts/python.exe" -m pip install -q -r "$root/client/requirements.txt"

cd "$root/client"
# pythonw = no console window pops up; Ctrl+C still works because the trap
# below kills by PID, not by console signal
"$root/.venv/Scripts/pythonw.exe" -m dragonrecorder &
app_pid=$!
# taskkill needs the Windows PID, not Git Bash's MSYS PID ($!). The winpid
# file isn't populated instantly, so retry briefly.
win_pid=""
for _ in 1 2 3 4 5 6 7 8; do
    win_pid="$(cat /proc/$app_pid/winpid 2>/dev/null || true)"
    [ -n "$win_pid" ] && break
    sleep 0.25
done
[ -n "$win_pid" ] || win_pid="$app_pid"

cleaned=0
cleanup() {
    [ "$cleaned" = 1 ] && return; cleaned=1
    taskkill //PID "$win_pid" //T //F >/dev/null 2>&1 || true
    kill "$app_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

dash_url="$(sed -n 's/^SERVER_URL=//p' "$root/.env" 2>/dev/null | tr -d '\r' | head -1)"
echo "DragonRecorder is running (Ctrl+C here to quit everything)."
echo "  Record hotkey: Ctrl+Alt+C (opens the panel)"
[ -n "$dash_url" ] && echo "  Library:       $dash_url/dash"
wait "$app_pid"
