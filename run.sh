#!/usr/bin/env bash
# DragonRecorder launcher - no flags. Git Bash flavor of run.ps1.
# Holds the app in the foreground: Ctrl+C (or closing this terminal) kills
# the whole tree - tray, bridge, webcam windows, ffmpeg, whisper.
set -e
root="$(cd "$(dirname "$0")" && pwd)"

# already running from a previous launch? just open its panel and exit
if curl -s -m 2 -X POST http://127.0.0.1:8477/open >/dev/null 2>&1; then
    echo "DragonRecorder is already running - opened its panel (top-right)."
    exit 0
fi

if [ ! -f "$root/.venv/Scripts/python.exe" ]; then
    echo "First run: creating virtualenv..."
    python -m venv "$root/.venv"
fi
"$root/.venv/Scripts/python.exe" -m pip install -q -r "$root/client/requirements.txt"

cd "$root/client"
# python.exe (not pythonw) so this stays attached to the terminal
"$root/.venv/Scripts/python.exe" -m dragonrecorder &
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

# on Ctrl+C, terminal close, or the app exiting on its own, force-kill the
# entire process tree (//T //F) so no ffmpeg/webview child is left behind
cleaned=0
cleanup() {
    [ "$cleaned" = 1 ] && return; cleaned=1
    taskkill //PID "$win_pid" //T //F >/dev/null 2>&1 || true
    kill "$app_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "DragonRecorder is running (Ctrl+C here to quit everything)."
echo "The record hotkey (Ctrl+Alt+C) opens the panel."
wait "$app_pid"
