"""The File -> Open procs: every files.list provider answers its own call. Run under
dbus-run-session (pixi run test-py does)."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import agentbus
from agentbus.freeq import Bridge

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


SERVER = os.environ.get("AGENTBUS_FREEQ_TEST_SERVER")


@unittest.skipUnless(SERVER, "set AGENTBUS_FREEQ_TEST_SERVER to a freeq server")
class BotsFilesTest(unittest.TestCase):
    def test_files_from_a_proc_in_bots(self):
        home = tempfile.mkdtemp()
        env = {**os.environ, "AGENTBUS_FREEQ_HOME": home, "AGENTBUS_FREEQ_SERVER": SERVER}
        os.environ["AGENTBUS_FREEQ_HOME"] = home
        hub = agentbus.Hub()
        # The #bots proc lives on its own bus, like another host.
        remote = subprocess.Popen(["dbus-run-session", "--", sys.executable, "demo.py", "--host", "files-t"],
                                  cwd=FILES, env=env, stdin=subprocess.PIPE)
        bridge = Bridge(server=SERVER).start()
        try:
            bridge.bot.wait_ready()
            app = agentbus.Procs()
            deadline = time.monotonic() + 30
            while not app.providers("files.list") and time.monotonic() < deadline:
                time.sleep(0.2)
            [(owner, _)] = app.providers("files.list")
            self.assertTrue(bridge.owners()[owner][0].startswith("files-t"))  # freeq may suffix a taken nick
            answer = app.call("files.list", provider=owner, path="plans/")
            self.assertEqual(answer["provider"], "#bots/files-t")
            self.assertEqual([f["name"] for f in answer["files"]], ["plans/kv-store-design.md"])
        finally:
            bridge.stop()
            remote.stdin.close()  # ends the other host's procs; see demo.other_host
            remote.wait()
            del hub
            shutil.rmtree(home)


if __name__ == "__main__":
    unittest.main()
