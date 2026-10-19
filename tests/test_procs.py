"""Functions and publishers from Python over a real bus. Run under dbus-run-session
(pixi run test-py does)."""

import json
import os
import subprocess
import sys
import threading
import time
import unittest

import agentbus

HUB = "org.latha.agentbus.test.PyProcs"
EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")
SCHEMA = json.dumps({"args": {"type": "object", "required": ["place"]}})


class ProcsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hub = agentbus.Hub(HUB)
        cls.proc = agentbus.Procs(HUB)
        cls.app = agentbus.Procs(HUB)

        @cls.proc.function("weather.current", schema=SCHEMA)
        def current(place):
            return {"temp_c": 11.5, "place": place}

        @cls.proc.function("math.div")
        def div(a, b):
            return {"q": a / b}

    def test_no_hub_says_how_to_start_one(self):
        procs = agentbus.Procs(HUB + ".Missing")
        with self.assertRaisesRegex(agentbus.HubNotRunning, "pixi run hub"):
            procs.provide("x.y", lambda args, info: {})
        with self.assertRaises(agentbus.BusError):
            procs.call("x.y")

    def test_call_by_name(self):
        self.assertEqual(self.app.call("weather.current", place="Berlin"), {"temp_c": 11.5, "place": "Berlin"})
        self.assertIn("weather.current", self.app.functions())
        [(owner, stats)] = self.app.providers("weather.current")
        self.assertEqual(owner, self.proc.unique_name)
        self.assertGreaterEqual(stats["calls"], 1)

    def test_errors(self):
        with self.assertRaises(agentbus.NoProvider):
            self.app.call("nobody.home")
        with self.assertRaises(agentbus.CallError) as bad_args:
            self.app.call("weather.current", city="Berlin")
        self.assertIn("InvalidArgs", str(bad_args.exception))
        with self.assertRaises(agentbus.CallError) as boom:
            self.app.call("math.div", a=1, b=0)
        self.assertIn("ZeroDivisionError", str(boom.exception))
        with self.assertRaises(TypeError):
            self.app.call("weather.current", place=object())

    def test_timeout_cancels(self):
        seen = threading.Event()

        def slow(args, info):
            while not info.cancelled:
                time.sleep(0.02)
            seen.set()
            return {}

        self.proc.provide("build.run", slow)
        with self.assertRaises(TimeoutError):
            self.app.call("build.run", timeout=0.3)
        self.assertTrue(seen.wait(5))
        self.proc.withdraw("build.run")
        with self.assertRaises(agentbus.NoProvider):
            self.app.call("build.run")

    def test_failover_and_lease(self):
        code = (
            "import agentbus, sys\n"
            f"p = agentbus.Procs({HUB!r})\n"
            "p.provide('echo.say', lambda a, i: {'from': 'child', **a})\n"
            "print('ready', flush=True)\n"
            "sys.stdin.read()\n"
        )
        child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        self.assertEqual(self.app.call("echo.say", x=1), {"from": "child", "x": 1})
        child.kill()
        child.wait()
        child.stdin.close()
        child.stdout.close()
        for _ in range(50):
            if "echo.say" not in self.app.functions():
                break
            time.sleep(0.05)
        self.assertNotIn("echo.say", self.app.functions())

    def test_publisher_follows_demand(self):
        started, stopped = threading.Event(), threading.Event()

        @self.proc.publisher("power.*")
        def feed(subject, stop):
            started.set()
            while not stop.wait(0.05):
                self.proc.publish("power.battery", subject, {"percent": 50.0})
            stopped.set()

        sub = self.app.subscribe("power.battery", "laptop")
        self.assertTrue(started.wait(5))
        event = sub.next_event(timeout=5)
        self.assertEqual((event["subject"], event["payload"]), ("laptop", {"percent": 50.0}))
        sub.unsubscribe()
        self.assertTrue(stopped.wait(5))

    def test_patterns_overlap(self):
        self.assertTrue(agentbus.patterns_overlap("weather.*", "weather.current"))
        self.assertTrue(agentbus.patterns_overlap("weather.current", "weather.*"))
        self.assertTrue(agentbus.patterns_overlap("*", "power.battery"))
        self.assertFalse(agentbus.patterns_overlap("weather.*", "power.*"))
        self.assertFalse(agentbus.patterns_overlap("weather.current", "weather.alert"))


class ExamplesTest(unittest.TestCase):
    def demo(self, *args):
        demo = subprocess.run([sys.executable, os.path.join(EXAMPLES, "procs_demo.py"), *args],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(demo.returncode, 0, demo.stderr)
        return demo.stdout

    def test_procs_demo(self):
        out = self.demo("Berlin", "--watch", "3")
        self.assertIn("app: weather.current(Berlin) = {", out)
        self.assertEqual(out.count("app: #"), 3)

    def test_app_exits_after_one_call(self):
        out = self.demo("Paris")
        self.assertIn("app: weather.current(Paris) = {", out)
        self.assertNotIn("app: #", out)
