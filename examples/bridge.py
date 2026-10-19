"""The bridge: makes every function announced in #bots on freeq callable on the local hub.

Run one per host, next to hub.py. Apps then call remote procs by function name, exactly like
local ones. AGENTBUS_FREEQ_SERVER picks the server (default ircs://irc.freeq.at:6697) and
AGENTBUS_TRUST, a comma-separated list of DIDs, limits whose procs are imported."""

import os
import time

import agentbus
from agentbus.freeq import Bridge

trust = [d for d in os.environ.get("AGENTBUS_TRUST", "").split(",") if d]
bridge = Bridge(agentbus.Procs(), trust=trust or None).start()
bridge.bot.wait_ready()
print(f"bridge: in {bridge.bot.channel} on {bridge.bot.server} as {bridge.bot.nick}", flush=True)

seen = set()
while True:
    now = bridge.functions()
    for name in sorted(now - seen):
        print(f"bridge: importing {name} from {', '.join(n for n, _ in bridge.providers(name))}", flush=True)
    for name in sorted(seen - now):
        print(f"bridge: {name} has no remote provider any more", flush=True)
    seen = now
    time.sleep(1)
