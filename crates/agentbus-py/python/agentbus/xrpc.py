"""Procs over HTTPS: a service's functions served as atproto XRPC methods, and calls to them.

Bus function ``weather.current`` is XRPC method ``org.latha.agentbus.weather.current``, which belongs to
service ``org.latha.agentbus.weather`` (the method's NSID minus its last segment). A proc serving that
service over HTTPS says so with one record in its atproto repo, in the collection named for the
service, rkey ``self``::

    {"$type": "org.latha.agentbus.weather", "endpoint": "https://…modal.run",
     "methods": ["org.latha.agentbus.weather.current"], "createdAt": "…"}

Brokers find every provider of a service through a relay's
``com.atproto.sync.listReposByCollection?collection=org.latha.agentbus.weather`` (see
``agentbus.relay``) and call ``GET <endpoint>/xrpc/org.latha.agentbus.weather.current?place=Berlin``.

A query's parameters arrive as strings; callers with typed (non-string) args POST a JSON object
instead, and the server accepts both. Errors are the XRPC envelope ``{"error", "message"}``.

Auth is atproto inter-service auth: the caller's PDS signs a short-lived JWT
(``com.atproto.server.getServiceAuth``) with ``iss`` = the caller's DID, ``aud`` = the proc's DID
and ``lxm`` = the method, sent as ``Authorization: Bearer <jwt>``. The proc checks it against the
``#atproto`` key in the caller's DID document. A ``Service`` without a DID serves unauthenticated.
"""

import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

PREFIX = "org.latha.agentbus."
PLC = os.environ.get("AGENTBUS_PLC", "https://plc.directory")
CALL_TIMEOUT = 20.0
MAX_BODY = 1 << 20
MAX_CLOCK_SKEW = 30  # seconds a token's iat may sit in the future
MAX_TOKEN_LIFETIME = 300

# The one place an error code meets its HTTP status.
STATUS = {
    "InvalidRequest": 400,
    "AuthRequired": 401,
    "InvalidToken": 401,
    "Forbidden": 403,
    "MethodNotImplemented": 501,
    "PayloadTooLarge": 413,
    "InternalServerError": 500,
    "UpstreamFailure": 502,
}


class XrpcError(Exception):
    """An XRPC error: ``code`` is machine-readable (a key of ``STATUS``)."""

    def __init__(self, code, message, status=None):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message
        self.status = status or STATUS.get(code, 500)


# ── Names ───────────────────────────────────────────────────────────────────


def method_nsid(function):
    """``weather.current`` -> ``org.latha.agentbus.weather.current``."""
    if function.count(".") < 1:
        raise ValueError(f"function {function!r} needs a service and a method, e.g. weather.current")
    return PREFIX + function


def function_name(nsid):
    """``org.latha.agentbus.weather.current`` -> ``weather.current``."""
    if not nsid.startswith(PREFIX):
        raise ValueError(f"{nsid!r} is not under {PREFIX}")
    return nsid[len(PREFIX):]


def service_of(nsid):
    """``org.latha.agentbus.weather.current`` -> ``org.latha.agentbus.weather``."""
    return nsid.rsplit(".", 1)[0]


# ── DIDs and keys ───────────────────────────────────────────────────────────


def _get_json(url, headers=None, timeout=CALL_TIMEOUT):
    req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def resolve_did(did, plc=None):
    """The DID document for a did:plc or did:web."""
    if did.startswith("did:plc:"):
        return _get_json(f"{(plc or PLC).rstrip('/')}/{quote(did, safe=':')}")
    if did.startswith("did:web:"):
        host = did[len("did:web:"):]
        if not host or ":" in host or "/" in host:
            raise ValueError(f"atproto did:web names a host only, not {did!r}")
        return _get_json(f"https://{host.replace('%3A', ':')}/.well-known/did.json")
    raise ValueError(f"unsupported DID method: {did!r}")


def _service(doc, fragment, kind):
    for svc in doc.get("service", []):
        if svc.get("id") in (fragment, doc.get("id", "") + fragment) and svc.get("type") == kind:
            return svc["serviceEndpoint"]
    raise LookupError(f"{doc.get('id')} has no {kind} service")


def pds_endpoint(doc):
    return _service(doc, "#atproto_pds", "AtprotoPersonalDataServer").rstrip("/")


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58btc(data):
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def _b58decode(text):
    n = 0
    for ch in text:
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def signing_key(doc):
    """The ``#atproto`` signing key of a DID document, as a cryptography public key, and its
    curve's JWT alg (ES256K for secp256k1, ES256 for P-256)."""
    from cryptography.hazmat.primitives.asymmetric import ec

    for vm in doc.get("verificationMethod", []):
        if vm.get("id") not in ("#atproto", doc.get("id", "") + "#atproto"):
            continue
        mb = vm.get("publicKeyMultibase", "")
        if not mb.startswith("z"):
            raise ValueError("#atproto key is not base58btc multibase")
        raw = _b58decode(mb[1:])
        if raw[:2] == b"\xe7\x01":
            return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), raw[2:]), "ES256K"
        if raw[:2] == b"\x80\x24":
            return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw[2:]), "ES256"
        raise ValueError("#atproto key is neither secp256k1 nor P-256")
    raise LookupError(f"{doc.get('id')} has no #atproto key")


def _b64decode(text):
    import base64

    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class ServiceAuth:
    """Checks inter-service JWTs addressed to ``did``. ``allow``, a set of DIDs, narrows who may
    call; ``None`` admits any caller with a valid atproto identity. DID documents are cached for
    ``ttl`` seconds."""

    def __init__(self, did, allow=None, resolve=resolve_did, ttl=300):
        self.did, self.allow, self.resolve, self.ttl = did, allow, resolve, ttl
        self._keys = {}  # did -> (fetched at, key, alg)
        self._lock = threading.Lock()

    def _key(self, did, refresh=False):
        with self._lock:
            hit = self._keys.get(did)
        if hit and not refresh and time.time() - hit[0] < self.ttl:
            return hit[1], hit[2]
        key, alg = signing_key(self.resolve(did))
        with self._lock:
            self._keys[did] = (time.time(), key, alg)
        return key, alg

    def check(self, authorization, lxm):
        """The caller's DID, or XrpcError."""
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        if not authorization or not authorization.startswith("Bearer "):
            raise XrpcError("AuthRequired", "send Authorization: Bearer <atproto service JWT>")
        token = authorization[len("Bearer "):].strip()
        try:
            h64, p64, s64 = token.split(".")
            header, claims, sig = json.loads(_b64decode(h64)), json.loads(_b64decode(p64)), _b64decode(s64)
        except ValueError:
            raise XrpcError("InvalidToken", "malformed JWT") from None
        now = time.time()
        iss = claims.get("iss")
        if not isinstance(iss, str) or not iss.startswith("did:"):
            raise XrpcError("InvalidToken", "iss must be a DID")
        iss = iss.split("#", 1)[0]
        if claims.get("aud") != self.did:
            raise XrpcError("InvalidToken", f"aud must be {self.did}")
        if claims.get("lxm") != lxm:
            raise XrpcError("InvalidToken", f"lxm must be {lxm}")
        exp, iat = claims.get("exp"), claims.get("iat", now)
        if not isinstance(exp, (int, float)) or exp <= now:
            raise XrpcError("InvalidToken", "token expired")
        if not isinstance(iat, (int, float)) or iat > now + MAX_CLOCK_SKEW or exp - iat > MAX_TOKEN_LIFETIME:
            raise XrpcError("InvalidToken", "token iat/exp out of range")
        if self.allow is not None and iss not in self.allow:
            raise XrpcError("Forbidden", f"{iss} may not call this service")
        if len(sig) != 64:
            raise XrpcError("InvalidToken", "signature must be 64 bytes (r||s)")
        r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
        signed = f"{h64}.{p64}".encode()
        for refresh in (False, True):  # a key rotation shows up as one failed check, then a refetch
            try:
                key, alg = self._key(iss, refresh)
            except Exception as e:
                raise XrpcError("InvalidToken", f"cannot resolve {iss}: {e}") from None
            if header.get("alg") != alg:
                raise XrpcError("InvalidToken", f"alg must be {alg} for {iss}'s key")
            try:
                key.verify(encode_dss_signature(r, s), signed, ec.ECDSA(hashes.SHA256()))
                return iss
            except InvalidSignature:
                continue
        raise XrpcError("InvalidToken", "bad signature")


# ── Client ──────────────────────────────────────────────────────────────────


def call(endpoint, function, args, token=None, timeout=CALL_TIMEOUT):
    """Call bus function ``function`` on the proc at ``endpoint``. String-only args go as a GET
    query, anything else as a POSTed JSON object. Returns the result dict or raises XrpcError."""
    url = f"{endpoint.rstrip('/')}/xrpc/{method_nsid(function)}"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if all(isinstance(v, str) for v in args.values()):
        req = urllib.request.Request(f"{url}?{urlencode(args)}" if args else url, headers=headers)
    else:
        req = urllib.request.Request(url, data=json.dumps(args).encode(), method="POST",
                                     headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
        except ValueError:
            err = {}
        raise XrpcError(err.get("error") or "UpstreamFailure", err.get("message") or str(e.reason),
                        e.code) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise XrpcError("UpstreamFailure", f"{url}: {e}") from None
    if not isinstance(result, dict):
        raise XrpcError("UpstreamFailure", f"{url} returned {type(result).__name__}, not an object")
    return result


# ── Server ──────────────────────────────────────────────────────────────────


class Service:
    """The methods of one service, served over HTTP::

        weather = Service("weather")   # org.latha.agentbus.weather

        @weather.method("current")     # org.latha.agentbus.weather.current, bus function weather.current
        def current(place):
            return {"temp_c": 11.5}

        weather.serve(8000, did="did:plc:…")
    """

    def __init__(self, name):
        self.nsid = PREFIX + name
        self.handlers = {}  # method NSID -> fn(**args) -> dict

    def method(self, name):
        def register(fn):
            self.handlers[f"{self.nsid}.{name}"] = fn
            return fn

        return register

    def record(self, endpoint):
        """This service's provider record (collection ``self.nsid``, rkey ``self``)."""
        return {"$type": self.nsid, "endpoint": endpoint.rstrip("/"), "methods": sorted(self.handlers),
                "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}

    def handle(self, nsid, args, authorization=None, auth=None):
        """Run one call: (status, body)."""
        try:
            fn = self.handlers.get(nsid)
            if fn is None:
                raise XrpcError("MethodNotImplemented", f"no method {nsid} here")
            caller = auth.check(authorization, nsid) if auth else None
            try:
                result = fn(**args)
            except (TypeError, ValueError) as e:
                raise XrpcError("InvalidRequest", str(e)) from None
            except XrpcError:
                raise
            except Exception:
                print(f"{nsid} failed for {caller or 'anonymous'}:\n{traceback.format_exc()}",
                      file=sys.stderr, flush=True)
                raise XrpcError("InternalServerError", f"{nsid} failed") from None
            if not isinstance(result, dict):
                print(f"{nsid} returned {type(result).__name__}, not a dict", file=sys.stderr, flush=True)
                raise XrpcError("InternalServerError", f"{nsid} failed")
            return 200, result
        except XrpcError as e:
            return e.status, {"error": e.code, "message": e.message}

    def server(self, port, host="0.0.0.0", did=None, allow=None, resolve=resolve_did):
        """An HTTP server for this service (not yet serving). With ``did``, every call needs a
        service JWT addressed to it."""
        service = self
        auth = ServiceAuth(did, allow, resolve) if did else None

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _route(self):
                parts = urlsplit(self.path)
                if not parts.path.startswith("/xrpc/"):
                    return None, parts
                return parts.path[len("/xrpc/"):], parts

            def do_GET(self):
                nsid, parts = self._route()
                if nsid == "_health":
                    return self._send(200, {"service": service.nsid, "methods": sorted(service.handlers)})
                if nsid is None:
                    return self._send(404, {"error": "NotFound", "message": "XRPC lives under /xrpc/"})
                args = dict(parse_qsl(parts.query, keep_blank_values=True))
                self._send(*service.handle(nsid, args, self.headers.get("Authorization"), auth))

            def do_POST(self):
                nsid, _ = self._route()
                if nsid is None:
                    return self._send(404, {"error": "NotFound", "message": "XRPC lives under /xrpc/"})
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._send(413, {"error": "PayloadTooLarge", "message": f"body over {MAX_BODY} bytes"})
                try:
                    args = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(args, dict):
                        raise ValueError
                except ValueError:
                    return self._send(400, {"error": "InvalidRequest", "message": "body must be a JSON object"})
                self._send(*service.handle(nsid, args, self.headers.get("Authorization"), auth))

            def log_message(self, fmt, *a):
                print(f"{service.nsid}: {self.address_string()} {fmt % a}", file=sys.stderr, flush=True)

        return ThreadingHTTPServer((host, port), Handler)

    def serve(self, port, host="0.0.0.0", did=None, allow=None):
        """Serve until killed."""
        self.server(port, host, did, allow).serve_forever()
