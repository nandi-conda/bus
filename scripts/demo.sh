#!/usr/bin/env bash
# Router + agent + sender on one bus. Run inside dbus-run-session (the pixi demo tasks do).
#   AGENT  command for the agent   (default: the Rust planner example)
#   args   command for the sender  (default: the Rust sender example)
set -euo pipefail
cargo build -q -p libagentbus --examples
bin=target/debug/examples
"$bin/router" & router=$!
${AGENT:-$bin/agent} & agent=$!
trap 'kill $router $agent 2>/dev/null' EXIT
sleep 1
"${@:-$bin/sender}"
