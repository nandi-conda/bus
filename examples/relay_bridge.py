"""The relay bridge: makes every proc serving the given services over HTTPS callable on the
local hub, found through an atproto relay. Run one per host, next to hub.py:

    python examples/relay_bridge.py weather files

AGENTBUS_RELAY picks the relay (default https://bsky.network), AGENTBUS_TRUST (comma-separated
DIDs) limits whose procs are imported. With AGENTBUS_PDS the bridge has its own rookery identity
(handle $AGENTBUS_HANDLE, default "bridge") and signs every call with a service JWT."""

import os
import sys
import time

from agentbus import relay, rookery

services = sys.argv[1:] or ["weather"]
trust = [d for d in os.environ.get("AGENTBUS_TRUST", "").split(",") if d]
identity = rookery.Identity.from_env("bridge") if os.environ.get("AGENTBUS_PDS") else None
bridge = relay.Bridge(services, relay=os.environ.get("AGENTBUS_RELAY", relay.DEFAULT_RELAY),
                      trust=trust or None, identity=identity).start()
who = f"as {identity.handle} ({identity.did})" if identity else "unauthenticated"
print(f"relay-bridge: following {', '.join(bridge.services)} on {bridge.relay}, calling {who}", flush=True)

seen = set()
while True:
    now = bridge.functions()
    for name in sorted(now - seen):
        print(f"relay-bridge: importing {name} from {', '.join(e for e, _ in bridge.providers(name))}", flush=True)
    for name in sorted(seen - now):
        print(f"relay-bridge: {name} has no provider any more", flush=True)
    seen = now
    time.sleep(1)
