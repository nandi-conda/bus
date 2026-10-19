"""Broadcast signals from Python. Run under dbus-run-session (pixi run test-py does)."""

import threading
import unittest

import agentbus

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
