"""A public proc against a real rookery: enrollment, its org.latha.agentbus.proc record, and its DID on
what it publishes. Needs a running rookery (e.g. `wrangler dev` in rookery-pds) named by
$AGENTBUS_TEST_PDS; skipped otherwise. Run under dbus-run-session (pixi run test-py does)."""

import json
import os
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

import agentbus

PDS = os.environ.get("AGENTBUS_TEST_PDS")
HUB = "org.latha.agentbus.test.PyPublic"


@unittest.skipUnless(PDS, "AGENTBUS_TEST_PDS not set")
class PublicProcTest(unittest.TestCase):
    def test_public_proc(self):
        hub = agentbus.Hub(HUB)  # noqa: F841 (serves while referenced)
        proc = agentbus.Procs(HUB, name="weather", description="test weather", public=True)

        @proc.function("weather.current")
        def current(place):
            return {"temp_c": 1.0}

        @proc.publisher("weather.*", topics=["weather.current", "weather.alert"], subjects="place")
        def feed(place, stop):
            stop.wait()

        handle = f"wx{int(time.time() * 1000):x}"
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(
            os.environ, {"AGENTBUS_PDS": PDS, "AGENTBUS_HANDLE": handle, "XDG_STATE_HOME": state}
        ):
            from agentbus import rookery

            with mock.patch.object(rookery, "STATE_DIR", rookery.Path(state) / "agentbus/procs"):
                ident = proc.go_public()
                # A second start loads the saved key instead of enrolling again.
                self.assertEqual(rookery.Identity.load_or_enroll("weather", PDS, handle).did, ident.did)

        url = f"{PDS}/xrpc/org.latha.agentbus.listProcs?did={ident.did}"
        [view] = json.load(urllib.request.urlopen(url))["procs"]
        self.assertEqual(view["value"]["provides"], [{"function": "weather.current"}])
        self.assertEqual(view["value"]["publishes"], [
            {"topic": "weather.current", "subjects": "place"}, {"topic": "weather.alert", "subjects": "place"}])

        sub = agentbus.Topics(HUB).subscribe("weather.current", "Oslo")
        proc.publish("weather.current", "Oslo", {"temp_c": 2.0})
        self.assertEqual(sub.next_event(timeout=5)["payload"]["source"], ident.did)


if __name__ == "__main__":
    unittest.main()
