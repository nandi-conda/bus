"""A proc's saved rookery identity follows its DID document when the account moves hosts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agentbus import rookery, xrpc

DID = "did:plc:movedproc"


def doc(pds):
    return {"id": DID, "service": [
        {"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": pds}]}


class SavedIdentity(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_dir = Path(tmp.name)
        self.state = state_dir / "weather"
        self.state.mkdir()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        (self.state / "key.pem").write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        for patch in (mock.patch.object(rookery, "STATE_DIR", state_dir),
                      mock.patch.object(rookery.Identity, "_request", return_value=b"terms")):
            patch.start()
            self.addCleanup(patch.stop)

    def load(self, saved_pds, did_pds):
        (self.state / "identity.json").write_text(
            json.dumps({"pds": saved_pds, "did": DID, "handle": "weather.agent.latha.org"}))
        with mock.patch.object(xrpc, "resolve_did", return_value=doc(did_pds)):
            # AGENTBUS_PDS still names the old host, as the procs' Modal secrets do.
            return rookery.Identity.load_or_enroll("weather", "https://old.example", "weather")

    def test_follows_a_moved_account(self):
        ident = self.load("https://old.example", "https://rookery.latha.org")
        self.assertEqual(ident.url, "https://rookery.latha.org")
        self.assertEqual(ident.origin, "https://rookery.latha.org")
        saved = json.loads((self.state / "identity.json").read_text())
        self.assertEqual(saved["pds"], "https://rookery.latha.org")
        self.assertEqual(saved["did"], DID)

    def test_keeps_a_local_url_for_the_same_host(self):
        ident = self.load("http://localhost:8787", "https://localhost:8787")
        self.assertEqual(ident.url, "http://localhost:8787")


if __name__ == "__main__":
    unittest.main()
