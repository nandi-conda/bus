#!/usr/bin/env bash
# Build the agentbus conda package (Rust core lib + PyO3 extension) for every
# platform and upload it to prefix.dev. Run in a cloud container, not a desktop:
#   pixi run -e build publish
# Auth: rattler-build reads the prefix.dev token from PREFIX_API_KEY or
# `pixi auth login prefix.dev --token ...`; in the cloud env the proxy injects it.
set -euo pipefail
cd "$(dirname "$0")/.."

channel="${PREFIX_CHANNEL:-nandi-testing}"
out="output"
# Build number = commit count on main, so each publish supersedes the last
# without bumping the Cargo version.
export AGENTBUS_BUILD_NUMBER="$(git rev-list --count HEAD)"

rm -rf "$out"
rattler-build build --experimental -r recipe -c conda-forge --output-dir "$out"
rattler-build build --experimental -r recipe -c conda-forge --output-dir "$out" \
  --target-platform linux-aarch64 --test skip

rattler-build upload prefix -c "$channel" -a "${PREFIX_API_KEY:-proxy-injected}" "$out"/linux-*/agentbus-*.conda
