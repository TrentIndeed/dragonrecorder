#!/usr/bin/env bash
# DragonRecorder launcher - no flags. Git Bash flavor of run.ps1.
set -e
root="$(cd "$(dirname "$0")" && pwd)"

# already running? just open its panel and be done
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
# pythonw = GUI subsystem, no console; output suppressed so log lines don't
# land in this shell; & + disown detaches
"$root/.venv/Scripts/pythonw.exe" -m dragonrecorder >/dev/null 2>&1 &
disown
echo "DragonRecorder is starting - the record hotkey (Ctrl+Alt+C) opens the panel."
