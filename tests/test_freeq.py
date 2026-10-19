"""Procs on freeq. The wire helpers always run. The end-to-end test needs a freeq server
(AGENTBUS_FREEQ_TEST_SERVER=irc://127.0.0.1:6667, a local `freeq-server`) and runs the public
proc on a second, private bus, so it stands in for another host."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import agentbus
from agentbus.freeq import Bridge, Identity, escape_tag, manifest_toml, parse_line, unescape_tag

SERVER = os.environ.get("AGENTBUS_FREEQ_TEST_SERVER")
HUB = "dev.agentbus.test.Freeq"


class WireTest(unittest.TestCase):
    def test_tag_escaping_round_trips(self):
        value = 'a; b\\c\r\n{"x": "y z"}'
        self.assertEqual(unescape_tag(escape_tag(value)), value)
        self.assertNotIn(" ", escape_tag(value))
        self.assertNotIn(";", escape_tag(value))

    def test_parse_line(self):
        tags, nick, command, params = parse_line(
            "@account=did:key:z6M;+agentbus/provides=weather.current,files.list "
            ":weather!w@host TAGMSG #bots")
        self.assertEqual(tags["+agentbus/provides"], "weather.current,files.list")
        self.assertEqual((nick, command, params), ("weather", "TAGMSG", ["#bots"]))
        self.assertEqual(parse_line(":srv 001 me :Welcome")[2:], ("001", ["me", "Welcome"]))

    def test_manifest_lists_functions_for_the_channel(self):
        toml = manifest_toml("weather", None, "did:plc:x", {"weather.current", "files.list"}, "#bots")
        self.assertIn('"#bots" = ["files.list", "weather.current"]', toml)
        self.assertIn('creator_did = "did:plc:x"', toml)

    def test_identity_persists(self):
        root = tempfile.mkdtemp()
        try:
            first = Identity.load("w", root)
            self.assertTrue(first.did.startswith("did:key:z6Mk"))
            self.assertEqual(Identity.load("w", root).did, first.did)
            self.assertEqual(os.stat(os.path.join(root, "w", "agent.key")).st_mode & 0o777, 0o600)
        finally:
            shutil.rmtree(root)


REMOTE_PROC = f"""
import agentbus, sys
hub = agentbus.Hub({HUB!r})
procs = agentbus.Procs({HUB!r}, name=sys.argv[1], freeq={{"server": {SERVER!r}}})

@procs.function("weather.current")
def current(place):
    return {{"temp_c": 11.5, "place": place, "from": sys.argv[1]}}

@procs.function("math.div")
def div(a, b):
    return {{"q": a / b}}

procs.go_freeq().bot.wait_ready()
print("ready", flush=True)
sys.stdin.read()
"""


@unittest.skipUnless(SERVER, "set AGENTBUS_FREEQ_TEST_SERVER to a freeq server")
class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        os.environ["AGENTBUS_FREEQ_HOME"] = cls.home
        cls.remotes = [cls.start_remote(f"weather{i}") for i in (1, 2)]
        cls.hub = agentbus.Hub(HUB)
        cls.app = agentbus.Procs(HUB)
        cls.bridge = Bridge(HUB, server=SERVER).start()
        cls.bridge.bot.wait_ready()
        cls.wait_for(lambda: len(cls.app.providers("weather.current")) == 2)

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()
        for proc in cls.remotes:
            cls.stop_remote(proc)
            proc.stdout.close()
        shutil.rmtree(cls.home)

    @classmethod
    def start_remote(cls, name):
        proc = subprocess.Popen(
            ["dbus-run-session", "--", sys.executable, "-c", REMOTE_PROC, name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        if proc.stdout.readline().strip() != "ready":
            raise RuntimeError(f"{name} did not come up")
        return proc

    @staticmethod
    def stop_remote(proc):
        # Closing stdin ends the script, which drops its freeq connection like a real exit.
        if not proc.stdin.closed:
            proc.stdin.close()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    @staticmethod
    def wait_for(cond, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.1)
        raise AssertionError("condition not met in time")

    def test_call_reaches_a_proc_in_bots(self):
        result = self.app.call("weather.current", place="Berlin")
        self.assertEqual((result["temp_c"], result["place"]), (11.5, "Berlin"))
        # Each remote proc is its own provider on the hub, so an app can fan out to all of them.
        owners = self.bridge.owners()
        senders = {}
        for owner, _ in self.app.providers("weather.current"):
            senders[self.app.call("weather.current", provider=owner, place="Oslo")["from"]] = owner
        self.assertEqual(set(senders), {"weather1", "weather2"})
        for name, owner in senders.items():
            nick, did = owners[owner]
            self.assertTrue(nick.startswith(name))
            self.assertTrue(did.startswith("did:key:"))

    def test_remote_error_comes_back(self):
        with self.assertRaises(agentbus.CallError) as err:
            self.app.call("math.div", a=1, b=0)
        self.assertIn("ZeroDivisionError", str(err.exception))

    def test_zz_leaving_withdraws(self):
        # Runs last: stops one remote, then the other.
        self.stop_remote(self.remotes[0])
        self.wait_for(lambda: len(self.app.providers("weather.current")) == 1)
        for _ in range(3):
            self.assertEqual(self.app.call("weather.current", place="Oslo")["from"], "weather2")
        self.stop_remote(self.remotes[1])
        self.wait_for(lambda: "weather.current" not in self.app.functions())


if __name__ == "__main__":
    unittest.main()
