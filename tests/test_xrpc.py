"""Procs over HTTPS: the XRPC service, inter-service auth, and the relay discovery, against a fake
relay, PLC and PDS on localhost. Run under dbus-run-session (pixi run test does)."""

import base64
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import agentbus
from agentbus import relay, xrpc

HUB = "org.latha.agentbus.test.Xrpc"
PROC_DID = "did:plc:weatherproc"
CALLER_DID = "did:plc:discoverycaller"


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Caller:
    """Stands in for a rookery account: a secp256k1 key whose PDS mints service JWTs."""

    def __init__(self, did):
        self.did = did
        self.key = ec.generate_private_key(ec.SECP256K1())
        point = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                  serialization.PublicFormat.CompressedPoint)
        self.doc = {"id": did, "verificationMethod": [{
            "id": f"{did}#atproto", "type": "Multikey", "controller": did,
            "publicKeyMultibase": "z" + xrpc._b58btc(b"\xe7\x01" + point)}]}

    def service_auth(self, aud, lxm, ttl=60, **claims):
        now = int(time.time())
        payload = {"iss": self.did, "aud": aud, "lxm": lxm, "iat": now, "exp": now + ttl, **claims}
        signing = f"{b64(json.dumps({'typ': 'JWT', 'alg': 'ES256K'}).encode())}.{b64(json.dumps(payload).encode())}"
        r, s = decode_dss_signature(self.key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        return f"{signing}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def weather_service():
    svc = xrpc.Service("weather")

    @svc.method("current")
    def current(place):
        if not place:
            raise ValueError("place must not be empty")
        return {"temp_c": 11.5, "place": place}

    @svc.method("boom")
    def boom():
        raise RuntimeError("secret internals")

    return svc


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


class ServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = weather_service().server(0, "127.0.0.1")
        cls.url = serve(cls.server)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_names(self):
        self.assertEqual(xrpc.method_nsid("weather.current"), "org.latha.agentbus.weather.current")
        self.assertEqual(xrpc.function_name("org.latha.agentbus.weather.current"), "weather.current")
        self.assertEqual(xrpc.service_of("org.latha.agentbus.weather.current"), "org.latha.agentbus.weather")
        with self.assertRaises(ValueError):
            xrpc.method_nsid("weather")

    def test_get_and_post(self):
        self.assertEqual(xrpc.call(self.url, "weather.current", {"place": "Berlin"}),
                         {"temp_c": 11.5, "place": "Berlin"})
        # Non-string args go as a JSON body.
        with self.assertRaises(xrpc.XrpcError) as err:
            xrpc.call(self.url, "weather.current", {"place": "Oslo", "tick": 1})
        self.assertEqual((err.exception.code, err.exception.status), ("InvalidRequest", 400))

    def test_errors(self):
        with self.assertRaises(xrpc.XrpcError) as err:
            xrpc.call(self.url, "weather.forecast", {"place": "Oslo"})
        self.assertEqual((err.exception.code, err.exception.status), ("MethodNotImplemented", 501))
        with self.assertRaises(xrpc.XrpcError) as err:
            xrpc.call(self.url, "weather.current", {"place": ""})
        self.assertEqual(err.exception.code, "InvalidRequest")
        with self.assertRaises(xrpc.XrpcError) as err:
            xrpc.call(self.url, "weather.boom", {})
        self.assertEqual(err.exception.code, "InternalServerError")
        self.assertNotIn("secret", err.exception.message)

    def test_record(self):
        record = weather_service().record("https://w.example/")
        self.assertEqual(record["$type"], "org.latha.agentbus.weather")
        self.assertEqual(record["endpoint"], "https://w.example")
        self.assertEqual(record["methods"], ["org.latha.agentbus.weather.boom", "org.latha.agentbus.weather.current"])


class AuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.caller = Caller(CALLER_DID)
        docs = {CALLER_DID: cls.caller.doc}
        cls.server = weather_service().server(0, "127.0.0.1", did=PROC_DID, resolve=docs.__getitem__)
        cls.url = serve(cls.server)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, token):
        return xrpc.call(self.url, "weather.current", {"place": "Berlin"}, token)

    def test_valid_token(self):
        token = self.caller.service_auth(PROC_DID, "org.latha.agentbus.weather.current")
        self.assertEqual(self.call(token)["place"], "Berlin")

    def test_rejections(self):
        lxm = "org.latha.agentbus.weather.current"
        stranger = Caller("did:plc:stranger")
        cases = {
            "AuthRequired": None,
            "InvalidToken": [
                "not.a.jwt",
                self.caller.service_auth("did:plc:someoneelse", lxm),
                self.caller.service_auth(PROC_DID, "org.latha.agentbus.weather.forecast"),
                self.caller.service_auth(PROC_DID, lxm, ttl=-10),
                self.caller.service_auth(PROC_DID, lxm, ttl=3600),
                # Signed by a key that isn't the caller's.
                stranger.service_auth(PROC_DID, lxm, iss=CALLER_DID),
                # A DID nobody can resolve.
                stranger.service_auth(PROC_DID, lxm),
            ],
        }
        with self.assertRaises(xrpc.XrpcError) as err:
            self.call(cases["AuthRequired"])
        self.assertEqual((err.exception.code, err.exception.status), ("AuthRequired", 401))
        for token in cases["InvalidToken"]:
            with self.assertRaises(xrpc.XrpcError) as err:
                self.call(token)
            self.assertEqual((err.exception.code, err.exception.status), ("InvalidToken", 401), token)

    def test_allow_list(self):
        auth = xrpc.ServiceAuth(PROC_DID, allow={"did:plc:other"}, resolve={CALLER_DID: self.caller.doc}.__getitem__)
        token = self.caller.service_auth(PROC_DID, "org.latha.agentbus.weather.current")
        with self.assertRaises(xrpc.XrpcError) as err:
            auth.check(f"Bearer {token}", "org.latha.agentbus.weather.current")
        self.assertEqual(err.exception.code, "Forbidden")


class FakeNetwork:
    """One local server playing relay, PLC directory and PDS."""

    def __init__(self):
        self.repos = {}  # collection -> [did]
        self.docs = {}  # did -> DID document
        self.records = {}  # (did, collection) -> record
        net = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if parts.path == "/xrpc/com.atproto.sync.listReposByCollection":
                    body = {"repos": [{"did": d} for d in net.repos.get(q["collection"], [])]}
                elif parts.path == "/xrpc/com.atproto.repo.getRecord" and (q["repo"], q["collection"]) in net.records:
                    body = {"uri": "at://x", "value": net.records[(q["repo"], q["collection"])]}
                elif parts.path.lstrip("/") in net.docs:
                    body = net.docs[parts.path.lstrip("/")]
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = serve(self.server)

    def add_provider(self, did, collection, record):
        self.repos.setdefault(collection, []).append(did)
        self.docs[did] = {"id": did, "service": [
            {"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": self.url}]}
        self.records[(did, collection)] = record


class RelayDiscoveryTest(unittest.TestCase):
    def test_discovers_calls_and_drops(self):
        caller = Caller(CALLER_DID)
        weather = weather_service().server(0, "127.0.0.1", did=PROC_DID, resolve={CALLER_DID: caller.doc}.__getitem__)
        endpoint = serve(weather)
        net = FakeNetwork()
        net.add_provider(PROC_DID, "org.latha.agentbus.weather", weather_service().record(endpoint))
        # A provider whose record points nowhere sensible is skipped, not imported.
        net.add_provider("did:plc:broken", "org.latha.agentbus.weather",
                         {"endpoint": "ftp://x", "methods": ["org.latha.agentbus.weather.current"]})

        hub = agentbus.Hub(HUB)  # noqa: F841 (serves while referenced)
        app = agentbus.Procs(HUB)
        discovery = relay.Discovery(["weather"], hub=HUB, relay=net.url, plc=net.url, identity=caller, refresh=3600)
        discovery.mirror.start()
        try:
            discovery.discover()
            self.assertEqual(discovery.functions(), {"weather.current", "weather.boom"})
            self.assertEqual(discovery.providers("weather.current"), [(endpoint, PROC_DID)])
            self.assertEqual(app.call("weather.current", place="Berlin"), {"temp_c": 11.5, "place": "Berlin"})
            with self.assertRaises(agentbus.CallError) as err:
                app.call("weather.boom")
            self.assertIn("InternalServerError", str(err.exception))

            net.repos["org.latha.agentbus.weather"].remove(PROC_DID)
            discovery.discover()
            self.assertEqual(discovery.functions(), set())
            self.assertNotIn("weather.current", app.functions())
        finally:
            discovery.mirror.stop()
            weather.shutdown()
            net.server.shutdown()


if __name__ == "__main__":
    unittest.main()
