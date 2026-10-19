#!/usr/bin/env bash
# File -> Open demo on one bus: a directory provider, an agent provider, and the GUI.
# Run inside dbus-run-session (pixi run files-demo does).
set -euo pipefail
cd "$(dirname "$0")/../examples/files"
python provider.py dir "local:crates" ../../crates & dir=$!
python provider.py agent "agent:planner" & agent=$!
trap 'kill $dir $agent 2>/dev/null' EXIT
python gui.py "$@"
