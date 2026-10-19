"""Python bindings over a real bus: a Python agent serving a Python client, directly and via
a slot router. Run under dbus-run-session (pixi run test-py does)."""

import subprocess
import sys
import time
import unittest

import agentbus

CLASS = "dev.agentbus.task.echo"


def echo(ctx):
    ctx.progress(50, "echoing")
    if ctx.params.get("reject"):
        raise agentbus.Rejected("asked to reject")
    if ctx.params.get("fail"):
        raise ValueError("asked to fail")
    answer = ctx.ask("say something")
    return {"goal": ctx.goal, "said": answer["text"], "n": ctx.params.get("n", 0)}


class AgentbusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = agentbus.Agent("org.agentbus.test.pyecho", [CLASS], echo)
        cls.client = agentbus.Client()

    def run_task(self, dest, params):
        task = self.client.submit(dest, CLASS, "echo me", params=params)
        events = []
        for event in task:
            events.append(event)
            if event["type"] == "question":
                self.assertEqual(task.state, "input-required")
                task.respond(event["id"], {"text": "hi"})
        return events, task.result()

    def test_direct(self):
        events, result = self.run_task("org.agentbus.test.pyecho", {"n": 3})
        self.assertEqual(events[0], {"type": "progress", "pct": 50.0, "note": "echoing"})
        self.assertEqual(events[-1], {"type": "finished", "state": "completed"})
        self.assertEqual(result["deliverable"], {"goal": "echo me", "said": "hi", "n": 3})

    def test_reject_and_fail(self):
        _, result = self.run_task("org.agentbus.test.pyecho", {"reject": True})
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(result["report"]["reason"], "asked to reject")
        _, result = self.run_task("org.agentbus.test.pyecho", {"fail": True})
        self.assertEqual(result["state"], "failed")
        self.assertIn("asked to fail", result["report"]["reason"])

    def test_via_router(self):
        router = agentbus.Router("org.agentbus.test.Echo1")
        self.agent.join_slot("org.agentbus.test.Echo1")
        events, result = self.run_task("org.agentbus.test.Echo1", {})
        self.assertEqual(result["state"], "completed")
        self.assertTrue(any(e["type"] == "log" for e in events))
        del router

    def test_demo(self):
        demo = subprocess.run(
            [sys.executable, "examples/demo.py", "zig"], capture_output=True, text=True
        )
        self.assertEqual(demo.returncode, 0, demo.stderr)
        self.assertIn("sender: finished completed", demo.stdout)
        self.assertIn("'language': 'zig'", demo.stdout)

    def test_greeter(self):
        greeter = subprocess.Popen([sys.executable, "examples/greeter.py"])
        try:
            time.sleep(1)
            for n in (1, 2):
                out = subprocess.run(
                    [sys.executable, "examples/greeter.py", "--call", "Maria"],
                    capture_output=True, text=True, check=True,
                ).stdout
                self.assertEqual(out.strip(), f"Hello Maria! I have been called {n} times.")
        finally:
            greeter.terminate()
            greeter.wait()

    def test_bad_input(self):
        with self.assertRaises(agentbus.BusError):
            self.client.submit("org.agentbus.test.pyecho", "dev.other", "x")
        with self.assertRaises(TypeError):
            self.client.submit("org.agentbus.test.pyecho", CLASS, "x", params={"x": object()})


if __name__ == "__main__":
    unittest.main()
