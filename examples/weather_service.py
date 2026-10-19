"""The weather proc as an HTTPS service: serves org.latha.agentbus.weather.current over XRPC, e.g.
GET /xrpc/org.latha.agentbus.weather.current?place=Berlin. This is what runs on Modal; brokers find it
through the relay and call it like a local proc (README: "Over HTTPS").

With $AGENTBUS_PDS it has its own rookery identity (handle $AGENTBUS_HANDLE, default "weather"):
it writes its provider record (collection org.latha.agentbus.weather, rkey self) naming
$AGENTBUS_ENDPOINT, and only answers callers presenting an atproto service JWT addressed to its
DID ($AGENTBUS_TRUST, comma-separated DIDs, narrows who). Without $AGENTBUS_PDS it serves
anyone, and nothing can discover it."""

import os
import sys
import threading

from agentbus import rookery, xrpc
from weather_readings import reading

weather = xrpc.Service("weather")


@weather.method("current")
def current(place):
    return reading(place)


port = int(os.environ.get("PORT", "8000"))
did = None
if os.environ.get("AGENTBUS_PDS"):
    endpoint = os.environ.get("AGENTBUS_ENDPOINT")
    if not endpoint:
        sys.exit("weather: AGENTBUS_ENDPOINT (this service's public https URL) is required with AGENTBUS_PDS")
    ident = rookery.Identity.from_env("weather")
    did = ident.did

    def publish():
        # Off the startup path: a cold start answers its first call without waiting on the PDS.
        ident.put_record(weather.nsid, "self", weather.record(endpoint))
        print(f"weather: {ident.handle} ({did}) provides {weather.nsid} at {endpoint}", flush=True)

    threading.Thread(target=publish, name="publish-record", daemon=True).start()
else:
    print("weather: no AGENTBUS_PDS, serving without identity: open to anyone, not discoverable",
          flush=True)

trust = {d for d in os.environ.get("AGENTBUS_TRUST", "").split(",") if d} or None
print(f"weather: listening on :{port}", flush=True)
weather.serve(port, did=did, allow=trust)
