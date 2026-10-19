"""The relay bridge: makes procs that serve agentbus services over HTTPS callable on the local hub.

For each service it follows (``weather`` = ``org.latha.agentbus.weather``) the bridge asks an atproto
relay which repos hold a record in that collection
(``com.atproto.sync.listReposByCollection``), resolves each repo's DID to its PDS, reads the
provider record (rkey ``self``: endpoint and methods, see ``agentbus.xrpc``) and gives every
provider its own connection to the local hub (``agentbus.mirror``). A local
``procs.call("weather.current", place="Berlin")`` then becomes
``GET <endpoint>/xrpc/org.latha.agentbus.weather.current?place=Berlin``.

With an ``identity`` (the bridge's own rookery account, ``agentbus.rookery.Identity``) each call
carries an inter-service JWT from its PDS, so procs know who is calling; without one, calls go
unauthenticated and only open procs answer. ``trust`` limits which provider DIDs are imported.
"""

import sys
import threading
import urllib.request
from urllib.parse import urlencode, urlsplit

from . import xrpc
from .mirror import Mirror

DEFAULT_RELAY = "https://bsky.network"
PAGE_LIMIT = 500
MAX_PAGES = 20  # at most 10 000 providers per service
REFRESH = 60.0


def _log(msg):
    print(f"relay-bridge: {msg}", file=sys.stderr, flush=True)


def _endpoint_ok(url):
    parts = urlsplit(url) if isinstance(url, str) else None
    if not parts or parts.query or parts.fragment or parts.username or parts.password:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1"))


def list_repos(relay, collection, timeout=xrpc.CALL_TIMEOUT):
    """Every DID the relay knows to hold a record in ``collection``."""
    dids, cursor = [], None
    for _ in range(MAX_PAGES):
        query = {"collection": collection, "limit": PAGE_LIMIT, **({"cursor": cursor} if cursor else {})}
        page = xrpc._get_json(f"{relay.rstrip('/')}/xrpc/com.atproto.sync.listReposByCollection?{urlencode(query)}",
                              timeout=timeout)
        dids.extend(r["did"] for r in page.get("repos", []) if isinstance(r.get("did"), str))
        cursor = page.get("cursor")
        if not cursor or not page.get("repos"):
            break
    return dids


def provider_record(did, service, plc=None, timeout=xrpc.CALL_TIMEOUT):
    """The validated provider record of ``did`` for ``service`` (an NSID), read from its PDS."""
    pds = xrpc.pds_endpoint(xrpc.resolve_did(did, plc))
    query = urlencode({"repo": did, "collection": service, "rkey": "self"})
    value = xrpc._get_json(f"{pds}/xrpc/com.atproto.repo.getRecord?{query}", timeout=timeout).get("value")
    if not isinstance(value, dict) or not _endpoint_ok(value.get("endpoint")):
        raise ValueError(f"{did}'s {service} record has no usable https endpoint")
    methods = value.get("methods")
    if not isinstance(methods, list) or not methods or not all(
            isinstance(m, str) and xrpc.service_of(m) == service and m != service for m in methods):
        raise ValueError(f"{did}'s {service} record lists no methods of {service}")
    return value


class Bridge:
    def __init__(self, services, hub=None, relay=DEFAULT_RELAY, trust=None, identity=None,
                 refresh=REFRESH, plc=None, call_timeout=xrpc.CALL_TIMEOUT):
        from . import HUB_NAME

        self.services = [s if s.startswith(xrpc.PREFIX) else xrpc.PREFIX + s for s in services]
        if not self.services:
            raise ValueError("the relay bridge needs at least one service to follow, e.g. weather")
        self.relay, self.trust, self.identity = relay, set(trust) if trust else None, identity
        self.refresh, self.plc, self.call_timeout = refresh, plc, call_timeout
        self.mirror = Mirror(hub or HUB_NAME, self._forward, log_prefix="relay-bridge")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="relay-discovery", daemon=True)

    def start(self):
        self.mirror.start()
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(10)
        self.mirror.stop()

    def functions(self):
        return self.mirror.functions()

    def providers(self, name):
        """[(endpoint, did)] of the procs providing ``name`` on the local hub."""
        return self.mirror.providers(name)

    def owners(self):
        return self.mirror.owners()

    def discover(self):
        """One discovery pass: bring the hub in line with what the relay says now. A service
        whose relay query fails keeps its current providers."""
        for service in self.services:
            try:
                dids = list_repos(self.relay, service)
            except Exception as e:
                _log(f"{self.relay} could not list {service} providers: {e}")
                continue
            found = set()
            for did in dids:
                if self.trust is not None and did not in self.trust:
                    continue
                try:
                    record = provider_record(did, service, self.plc)
                except Exception as e:
                    _log(f"skipping {did} for {service}: {e}")
                    continue
                key = (did, service)
                found.add(key)
                self.mirror.put(key, record["endpoint"], did, {xrpc.function_name(m) for m in record["methods"]})
            with self.mirror.lock:
                stale = [k for k in self.mirror.remotes if k[1] == service and k not in found]
            for key in stale:
                self.mirror.drop(key)
        self.mirror.wait_synced()

    def _loop(self):
        while not self._stop.is_set():
            self.discover()
            self._stop.wait(self.refresh)

    def _forward(self, remote, function, args):
        token = None
        if self.identity:
            token = self.identity.service_auth(remote.did, xrpc.method_nsid(function))
        try:
            return xrpc.call(remote.label, function, args, token, self.call_timeout)
        except xrpc.XrpcError as e:
            raise RuntimeError(f"{remote.label} ({remote.did}): {e}") from None
