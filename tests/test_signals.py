"""Broadcast signals from Python, including the File -> Open providers. Run under
dbus-run-session (pixi run test-py does)."""

import os
import subprocess
import sys
import threading
import time
import unittest

import agentbus

FILES = os.path.join(os.path.dirname(__file__), "..", "examples", "files")
PATH, IFACE = "/org/example/Files", "org.example.Files1"


class SignalsTest(unittest.TestCase):
    def test_broadcast_and_listen(self):
        a, b = agentbus.Signals(), agentbus.Signals()
        listener = b.listen(PATH, IFACE, "Typed")
        a.broadcast(PATH, IFACE, "Typed", "s", 3, 1.5, True, ["x", "y"], {"k": [1]})
        s = listener.next_signal(timeout=5)
        self.assertEqual(s["sender"], a.unique_name)
        self.assertEqual(s["member"], "Typed")
        self.assertEqual(s["args"], ["s", 3, 1.5, True, ["x", "y"], {"k": [1]}])
        listener.close()
        with self.assertRaises(agentbus.BusError):
            listener.next_signal(timeout=1)

    def test_on_signal_callback(self):
        bus = agentbus.Signals()
        got = []
        done = threading.Event()
        stop = agentbus.on_signal(bus, PATH, IFACE, "Ping", lambda s: (got.append(s["args"]), done.set()))
        agentbus.Signals().broadcast(PATH, IFACE, "Ping", "hi")
        self.assertTrue(done.wait(5))
        stop()
        self.assertEqual(got, [["hi"]])

    def test_file_providers_answer_get_files(self):
        py = sys.executable
        providers = [
            subprocess.Popen([py, "provider.py", "dir", "local:files", "."], cwd=FILES),
            subprocess.Popen([py, "provider.py", "bot", "bot:notes"], cwd=FILES),
        ]
        try:
            time.sleep(1)
            bus = agentbus.Signals()
            replies = bus.listen(PATH, IFACE, "SendFiles")
            bus.broadcast(PATH, IFACE, "GetFiles", "req-1")
            got = {}
            while len(got) < 2:
                request, provider, files = replies.next_signal(timeout=5)["args"]
                self.assertEqual(request, "req-1")
                got[provider] = files
            self.assertIn("provider.py", got["local:files"])
            self.assertIn("plans/kv-store-design.md", got["bot:notes"])
        finally:
            for p in providers:
                p.terminate()
                p.wait()
