"""Own the hub: apps call functions and subscribe to topics through it.

The hub also reaches procs that serve agentbus services over HTTPS: for each service in
$AGENTBUS_SERVICES (comma-separated, default "weather") it asks the relay ($AGENTBUS_RELAY,
default https://bsky.network) who provides it and makes each provider callable here, so
procs.call("weather.current", place="Berlin") reaches a proc on Modal like a local one.
AGENTBUS_TRUST (comma-separated DIDs) limits whose procs are used. The hub has its own rookery
identity on $AGENTBUS_PDS (default the project's rookery on Modal), enrolled on first start as
$AGENTBUS_HANDLE (default "hub-" + a hash of this machine's id, so hubs on different machines
don't collide), and signs every call with an atproto service JWT."""

import hashlib
import os
import threading
import time
from pathlib import Path

import agentbus
from agentbus import relay, rookery

PDS = "https://codegod100--rookery-pds-web.us-east.modal.run"
# Remote procs scale to zero on Modal; a cold start can take ~35 s, so wait past it.
CALL_TIMEOUT = 60


def env_list(name, default=""):
    return [v for v in os.environ.get(name, default).split(",") if v]


hub = agentbus.Hub()
print(f"hub: serving {agentbus.HUB_NAME}", flush=True)

services = env_list("AGENTBUS_SERVICES", "weather")


def follow_remote():
    """Sign in to rookery (which may be waking from zero) and follow the relay; local procs
    work through the hub meanwhile."""
    os.environ.setdefault("AGENTBUS_PDS", PDS)
    machine = hashlib.sha256(Path("/etc/machine-id").read_bytes().strip()).hexdigest()[:8]
    os.environ.setdefault("AGENTBUS_HANDLE", f"hub-{machine}")
    while True:
        try:
            identity = rookery.Identity.from_env("hub")
            break
        except (OSError, rookery.RookeryError) as e:
            if isinstance(e, rookery.RookeryError) and e.status < 500:
                raise  # a real refusal (e.g. HandleTaken), not rookery still waking
            print(f"hub: can't sign in to {os.environ['AGENTBUS_PDS']} yet ({e}); retrying in 10s", flush=True)
            time.sleep(10)
    discovery = relay.Discovery(services, relay=os.environ.get("AGENTBUS_RELAY", relay.DEFAULT_RELAY),
                                trust=env_list("AGENTBUS_TRUST") or None, identity=identity,
                                call_timeout=CALL_TIMEOUT).start()
    print(f"hub: following {', '.join(discovery.services)} on {discovery.relay}, "
          f"calling as {identity.handle} ({identity.did})", flush=True)


if services:
    threading.Thread(target=follow_remote, name="follow-remote", daemon=True).start()

hub.run_forever()
