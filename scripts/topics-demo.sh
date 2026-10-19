#!/usr/bin/env bash
# Topics hub + weather source + an app watching Berlin, all in Python. Run inside
# dbus-run-session (`pixi run topics-demo` does). The source only produces Berlin while the app
# watches it.
set -euo pipefail
python examples/topics_hub.py & hub=$!
sleep 0.5
python examples/weather_source.py & source=$!
trap 'kill $hub $source 2>/dev/null' EXIT
sleep 0.5
python examples/watch.py "weather.*" Berlin "${COUNT:-5}"
sleep 0.5
