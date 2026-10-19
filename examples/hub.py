"""Own the hub: apps call functions and subscribe to topics through it.

The hub also reaches procs that serve agentbus services over HTTPS: for each service in
$AGENTBUS_SERVICES (comma-separated, default "weather,files") it asks the relay ($AGENTBUS_RELAY,
default https://bsky.network) who provides it and makes each provider callable here, so
procs.call("weather.current", place="Berlin") reaches a proc on Modal like a local one.
AGENTBUS_TRUST (comma-separated DIDs) limits whose procs are used. The hub has its own rookery
identity on $AGENTBUS_PDS (default the project's rookery, https://rookery.latha.org), enrolled on first start as
$AGENTBUS_HANDLE (default "hub-" + a hash of this machine's id, so hubs on different machines
don't collide), and signs every call with an atproto service JWT.

It provides hub.remotes, {"remotes": [{owner, did, handle, endpoint}]}: who each remote provider
is, so the mount (examples/files/mount.py) can name its folder by handle. ``handle`` is the one
the DID document claims, confirmed by the DID's own PDS; it is left out when that fails or
another remote claims the same handle."""

import hashlib
import os
import sys
import threading
import time
from pathlib import Path

import agentbus
from agentbus import relay, rookery, xrpc
from urllib.parse import urlencode

PDS = "https://rookery.latha.org"
# Remote procs scale to zero on Modal; a cold start can take ~35 s, so wait past it.
CALL_TIMEOUT = 60
HANDLE_TTL = 600  # seconds a DID's handle is trusted before it is resolved again


def env_list(name, default=""):
    return [v for v in os.environ.get(name, default).split(",") if v]


hub = agentbus.Hub()
print(f"hub: serving {agentbus.HUB_NAME}", flush=True)

services = env_list("AGENTBUS_SERVICES", "weather,files")
discovery = None  # relay.Discovery, once the hub has signed in
handles = {}  # did -> (handle or None, resolved at)
handles_lock = threading.Lock()


def resolve_handle(did):
    """The handle ``did``'s document claims (alsoKnownAs at://<handle>) if its PDS resolves that
    handle back to ``did``; else None."""
    try:
        doc = xrpc.resolve_did(did)
        claimed = next((a[len("at://"):] for a in doc.get("alsoKnownAs", []) if a.startswith("at://")), None)
        if not claimed:
            return None
        query = urlencode({"handle": claimed})
        back = xrpc._get_json(f"{xrpc.pds_endpoint(doc)}/xrpc/com.atproto.identity.resolveHandle?{query}")
        return claimed.lower() if back.get("did") == did else None
    except Exception as e:
        print(f"hub: can't resolve {did}'s handle: {e}", file=sys.stderr, flush=True)
        return None


def handle_of(did):
    with handles_lock:
        hit = handles.get(did)
    if hit and time.monotonic() - hit[1] < HANDLE_TTL:
        return hit[0]
    handle = resolve_handle(did)
    with handles_lock:
        handles[did] = (handle, time.monotonic())
    return handle


def remotes(args, info):
    owners = discovery.owners() if discovery else {}
    found = [{"owner": o, "did": did, "handle": handle_of(did), "endpoint": endpoint}
             for o, (endpoint, did) in sorted(owners.items())]
    claims = {}
    for r in found:
        if r["handle"]:
            claims.setdefault(r["handle"], set()).add(r["did"])
    for r in found:
        if not r["handle"] or len(claims[r["handle"]]) > 1:
            del r["handle"]  # none confirmed, or two DIDs claim it: neither gets the name
    return {"remotes": found}


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
    global discovery
    discovery = relay.Discovery(services, relay=os.environ.get("AGENTBUS_RELAY", relay.DEFAULT_RELAY),
                                trust=env_list("AGENTBUS_TRUST") or None, identity=identity,
                                call_timeout=CALL_TIMEOUT).start()
    print(f"hub: following {', '.join(discovery.services)} on {discovery.relay}, "
          f"calling as {identity.handle} ({identity.did})", flush=True)


if services:
    # Before discovery starts, so the mount can name every remote provider it sees.
    identities = agentbus.Procs()
    identities.provide("hub.remotes", remotes)
    threading.Thread(target=follow_remote, name="follow-remote", daemon=True).start()

hub.run_forever()
