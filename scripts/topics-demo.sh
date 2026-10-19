#!/usr/bin/env bash
# Topics hub + weather source + an app watching Berlin. Run inside dbus-run-session
# (`pixi run topics-demo` does). The source only produces Berlin while the app watches it.
set -euo pipefail
cargo build -q -p libagentbus --examples
bin=target/debug/examples
"$bin/topics_hub" & hub=$!
sleep 0.5
"$bin/weather_source" & source=$!
trap 'kill $hub $source 2>/dev/null' EXIT
sleep 0.5
"$bin/watch" "weather.*" Berlin "${COUNT:-5}"
sleep 0.5
