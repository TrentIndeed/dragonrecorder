#!/usr/bin/env bash
# DragonRecorder launcher - no flags. Git Bash flavor of run.ps1.
set -e
root="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$root/.venv/Scripts/python.exe" ]; then
    echo "First run: creating virtualenv..."
    python -m venv "$root/.venv"
fi
"$root/.venv/Scripts/python.exe" -m pip install -q -r "$root/client/requirements.txt"

cd "$root/client"
# pythonw = GUI subsystem, no console; & + disown detaches from this shell
"$root/.venv/Scripts/pythonw.exe" -m dragonrecorder &
disown
echo "DragonRecorder is running - look for the tray icon. The record hotkey opens the panel."
