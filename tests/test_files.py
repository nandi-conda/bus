"""The File -> Open procs: every files.list provider answers its own call. Run under
dbus-run-session (pixi run test-py does)."""

import os
import subprocess
import sys
import time
import unittest

import agentbus

FILES = os.path.join(os.path.dirname(__file__), "..", "examples", "files")


class FilesTest(unittest.TestCase):
    def test_fan_out_to_every_provider(self):
        hub = agentbus.Hub()  # the procs use the default hub name
        py = sys.executable
        procs = [
            subprocess.Popen([py, "provider.py", "dir", "local:files", "."], cwd=FILES),
            subprocess.Popen([py, "provider.py", "bot", "bot:notes"], cwd=FILES),
        ]
        try:
            app = agentbus.Procs()
            deadline = time.monotonic() + 10
            while len(app.providers("files.list")) < 2 and time.monotonic() < deadline:
                time.sleep(0.1)
            got, owners = {}, {}
            for owner, _ in app.providers("files.list"):
                answer = app.call("files.list", provider=owner)
                got[answer["provider"]] = {f["name"]: f for f in answer["files"]}
                owners[answer["provider"]] = owner
            self.assertEqual(set(got), {"local:files", "bot:notes"})
            self.assertEqual(got["local:files"]["provider.py"]["mime"], "text/x-python")
            self.assertTrue(got["bot:notes"]["plans/kv-store-design.md"]["uri"].startswith("bot://"))

            notes = app.call("files.list", provider=owners["bot:notes"], path="plans/")
            self.assertEqual([f["name"] for f in notes["files"]], ["plans/kv-store-design.md"])
            with self.assertRaises(agentbus.NoProvider):
                app.call("files.list", provider=app.unique_name)
        finally:
            for p in procs:
                p.terminate()
                p.wait()
            del hub


if __name__ == "__main__":
    unittest.main()
