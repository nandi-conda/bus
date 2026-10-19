"""A proc's own atproto identity on rookery-pds (PROCS.md §4.1, DESIGN.md §6b).

rookery has no passwords: an RSA-4096 key *is* the account credential (WelcomeMat). The first run
enrolls a handle with that key; later runs load the key and rebuild the access token from the
current terms of service. Every write carries `Authorization: DPoP <wm+jwt>` and a fresh
`DPoP: <dpop+jwt>` proof bound to the access token (`ath`).

The PDS's public origin (`https://<host>`) is what tokens and proofs name (`aud`, `htu`); `url`
is where requests go, which differs only when talking to a local `wrangler dev`.
"""

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PROC_COLLECTION = "org.latha.agentbus.proc"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agentbus/procs"


class RookeryError(Exception):
    """An XRPC error from rookery: `code` is its machine-readable error, e.g. InvalidRecord."""

    def __init__(self, status, code, message):
        super().__init__(f"rookery {status} {code}: {message}")
        self.status, self.code = status, code


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _uint(n: int) -> str:
    return _b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


class Identity:
    """One proc's rookery account: its key, DID and handle, and signed XRPC calls."""

    def __init__(self, url: str, key: rsa.RSAPrivateKey, did: str | None = None, handle: str | None = None):
        self.url = url.rstrip("/")
        self.origin = f"https://{urlsplit(self.url).netloc}"
        self.key, self.did, self.handle = key, did, handle
        pub = key.public_key().public_numbers()
        self.jwk = {"kty": "RSA", "n": _uint(pub.n), "e": _uint(pub.e)}
        # RFC 7638: required members in lexicographic order, no whitespace.
        canonical = json.dumps({"e": self.jwk["e"], "kty": "RSA", "n": self.jwk["n"]}, separators=(",", ":"))
        self.thumbprint = _b64(hashlib.sha256(canonical.encode()).digest())
        self.tos = self._request("GET", "/tos", raw=True).decode()
        self.access_token = self._jwt(
            {"typ": "wm+jwt", "alg": "RS256"},
            {"tos_hash": _b64(hashlib.sha256(self.tos.encode()).digest()), "aud": self.origin,
             "cnf": {"jkt": self.thumbprint}, "iat": int(time.time())},
        )

    @classmethod
    def load_or_enroll(cls, name: str, url: str, handle: str, invite: str | None = None) -> "Identity":
        """The proc's identity, enrolling `handle` on first use. State lives in
        $XDG_STATE_HOME/agentbus/procs/<name>/ (key.pem mode 0600, identity.json).

        The key is saved before signing up, so a signup whose answer got lost (a timeout, a
        crash) is picked up on the next start through getSession instead of stranding the
        handle on an account nobody holds the key to."""
        state = STATE_DIR / name
        key_file, id_file = state / "key.pem", state / "identity.json"
        if id_file.exists():
            key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
            saved = json.loads(id_file.read_text())
            if saved["pds"] != url.rstrip("/"):
                raise RookeryError(0, "WrongPds", f"{name} is enrolled on {saved['pds']}, not {url}")
            return cls(url, key, saved["did"], saved["handle"])

        if key_file.exists():
            key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        else:
            key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
            state.mkdir(parents=True, exist_ok=True)
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        ident = cls(url, key)
        try:
            session = ident._request("GET", "/xrpc/com.atproto.server.getSession",
                                     headers=ident._auth("GET", "/xrpc/com.atproto.server.getSession"))
            ident.did, ident.handle = session["did"], session["handle"]
        except RookeryError as e:
            if e.code != "AccountNotFound":
                raise
            ident._enroll(handle, invite)
        id_file.write_text(json.dumps({"pds": ident.url, "did": ident.did, "handle": ident.handle}))
        return ident

    def _enroll(self, handle: str, invite: str | None) -> None:
        body = {"handle": handle, "tos_signature": _b64(self._sign(self.tos.encode())),
                "access_token": self.access_token}
        if invite:
            body["ref"] = invite
        out = self._request("POST", "/api/signup", body, headers={"DPoP": self._proof("POST", "/api/signup")})
        self.did, self.handle = out["did"], out["handle"]

    @classmethod
    def from_env(cls, name: str) -> "Identity":
        """``load_or_enroll`` from $AGENTBUS_PDS, $AGENTBUS_HANDLE (default ``name``) and
        $AGENTBUS_INVITE."""
        pds = os.environ.get("AGENTBUS_PDS")
        if not pds:
            raise RuntimeError(f"{name} needs AGENTBUS_PDS (e.g. https://rook.host) for its identity")
        return cls.load_or_enroll(name, pds, os.environ.get("AGENTBUS_HANDLE", name),
                                  os.environ.get("AGENTBUS_INVITE"))

    def service_auth(self, aud: str, lxm: str, ttl: int = 60) -> str:
        """An atproto inter-service JWT from our PDS: we (``iss``) call method ``lxm`` on ``aud``."""
        path = "/xrpc/com.atproto.server.getServiceAuth"
        query = urlencode({"aud": aud, "lxm": lxm, "exp": int(time.time()) + ttl})
        # RFC 9449: htu is the request URL without its query.
        return self._request("GET", f"{path}?{query}", headers=self._auth("GET", path))["token"]

    def put_record(self, collection: str, rkey: str, record: dict) -> dict:
        return self.xrpc("com.atproto.repo.putRecord",
                         {"repo": self.did, "collection": collection, "rkey": rkey, "record": record})

    def xrpc(self, method: str, body: dict) -> dict:
        path = f"/xrpc/{method}"
        return self._request("POST", path, body, headers=self._auth("POST", path))

    def _auth(self, method: str, path: str) -> dict:
        """Headers for a DPoP-bound request; ``path`` without its query (RFC 9449 htu)."""
        return {
            "Authorization": f"DPoP {self.access_token}",
            "DPoP": self._proof(method, path, ath=_b64(hashlib.sha256(self.access_token.encode()).digest())),
        }

    def _sign(self, data: bytes) -> bytes:
        return self.key.sign(data, padding.PKCS1v15(), hashes.SHA256())

    def _jwt(self, header: dict, claims: dict) -> str:
        signing_input = ".".join(_b64(json.dumps(p, separators=(",", ":")).encode()) for p in (header, claims))
        return f"{signing_input}.{_b64(self._sign(signing_input.encode()))}"

    def _proof(self, htm: str, path: str, ath: str | None = None) -> str:
        claims = {"jti": str(uuid.uuid4()), "htm": htm, "htu": self.origin + path, "iat": int(time.time())}
        if ath:
            claims["ath"] = ath
        return self._jwt({"typ": "dpop+jwt", "alg": "RS256", "jwk": self.jwk}, claims)

    def _request(self, method, path, body=None, headers=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=dict(headers or {}))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except ValueError:
                err = {}
            raise RookeryError(e.code, err.get("error", "HTTPError"), err.get("message", e.reason)) from None
        return payload if raw else json.loads(payload)


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
