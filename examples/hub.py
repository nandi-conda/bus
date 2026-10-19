"""Own the hub: apps call functions and subscribe to topics through it.

The hub also reaches procs that serve agentbus services over HTTPS: for each service in
$AGENTBUS_SERVICES (comma-separated, default "weather") it asks the relay ($AGENTBUS_RELAY,
default https://bsky.network) who provides it and makes each provider callable here, so
procs.call("weather.current", place="Berlin") reaches a proc on Modal like a local one.
AGENTBUS_TRUST (comma-separated DIDs) limits whose procs are used. With $AGENTBUS_PDS the hub
has its own rookery identity (handle $AGENTBUS_HANDLE, default "hub") and signs every call with
an atproto service JWT; without it, only procs open to anyone answer."""

import os

import agentbus
from agentbus import relay, rookery


def env_list(name, default=""):
    return [v for v in os.environ.get(name, default).split(",") if v]


hub = agentbus.Hub()
print(f"hub: serving {agentbus.HUB_NAME}", flush=True)

services = env_list("AGENTBUS_SERVICES", "weather")
if services:
    identity = rookery.Identity.from_env("hub") if os.environ.get("AGENTBUS_PDS") else None
    discovery = relay.Discovery(services, relay=os.environ.get("AGENTBUS_RELAY", relay.DEFAULT_RELAY),
                                trust=env_list("AGENTBUS_TRUST") or None, identity=identity).start()
    who = f"as {identity.handle} ({identity.did})" if identity else "unauthenticated"
    print(f"hub: following {', '.join(discovery.services)} on {discovery.relay}, calling {who}", flush=True)

hub.run_forever()
