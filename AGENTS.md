# AGENTS.md

## Modal: always scale to zero

Modal services must always scale to zero. Never set `min_containers`, `keep_warm`, or anything else that leaves containers running when idle. Handle cold starts with timeouts and faster startup instead.
