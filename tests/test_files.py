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
    def test_content_encodings(self):
        sys.path.insert(0, FILES)
        from common import MAX_READ, content_bytes, file_content

        png = bytes([0x89, 0x50, 0x4E, 0x47, 0xFF, 0x00])
        answer = file_content("dot.png", png)
        self.assertEqual((answer["encoding"], answer["mime"]), ("base64", "image/png"))
        self.assertEqual(content_bytes(answer), png)
        self.assertEqual(content_bytes(file_content("a.txt", "héllo".encode())), "héllo".encode())
        with self.assertRaises(ValueError):
            file_content("big.bin", bytes(MAX_READ + 1))

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

            local = owners["local:files"]
            src = app.call("files.read", provider=local, file="provider.py")
            self.assertEqual(src["encoding"], "utf-8")
            self.assertIn("files.read", src["content"])
            self.assertEqual(src["size"], len(src["content"].encode()))
            note = app.call("files.read", provider=owners["bot:notes"], file="plans/kv-store-design.md")
            self.assertTrue(note["content"].startswith("# KV store"))
            for bad in ("../../README.md", "/etc/passwd", "missing.txt", ""):
                with self.assertRaises(agentbus.CallError, msg=bad):
                    app.call("files.read", provider=local, file=bad)
        finally:
            for p in procs:
                p.terminate()
                p.wait()
            del hub


if __name__ == "__main__":
    unittest.main()
