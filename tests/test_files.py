"""The File -> Open procs: every files.list provider answers its own call. Run under
dbus-run-session (pixi run test-py does)."""

import ctypes.util
import os
import shutil
import subprocess
import sys
import tempfile
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

    def test_manage_files(self):
        sys.path.insert(0, FILES)
        from common import content_bytes, write_args

        hub = agentbus.Hub()
        root = tempfile.mkdtemp()
        proc = subprocess.Popen([sys.executable, "provider.py", "dir", "docs", root], cwd=FILES)
        try:
            app = agentbus.Procs()
            deadline = time.monotonic() + 10
            while not app.providers("files.rename") and time.monotonic() < deadline:
                time.sleep(0.1)
            entry = app.call("files.write", file="notes/a.txt", **write_args("héllo".encode()))
            self.assertEqual((entry["name"], entry["size"]), ("notes/a.txt", 6))
            app.call("files.write", file="pic.png", **write_args(b"\x89PNG\xff"))
            self.assertEqual(content_bytes(app.call("files.read", file="pic.png")), b"\x89PNG\xff")
            app.call("files.mkdir", dir="empty")
            listing = app.call("files.list")
            self.assertEqual(sorted(listing["dirs"]), ["empty", "notes"])
            self.assertEqual(app.call("files.rename", file="notes", to="kept/notes"), {"name": "kept/notes"})
            self.assertEqual(read(os.path.join(root, "kept/notes/a.txt")), "héllo")
            for name, args in (("files.rmdir", {"dir": "kept"}), ("files.delete", {"file": "nope"}),
                               ("files.write", {"file": "../out.txt", **write_args(b"")}),
                               ("files.rename", {"file": "kept", "to": "kept/in"}),
                               ("files.mkdir", {"dir": "empty"})):
                with self.assertRaises(agentbus.CallError, msg=(name, args)):
                    app.call(name, **args)
            app.call("files.delete", file="kept/notes/a.txt")
            for d in ("kept/notes", "kept", "empty"):
                app.call("files.rmdir", dir=d)
            self.assertEqual(sorted(os.listdir(root)), ["pic.png"])
        finally:
            proc.terminate()
            proc.wait()
            shutil.rmtree(root)
            del hub


def can_mount():
    """FUSE needs /dev/fuse, libfuse3 and fusermount3 (which mount.py --unmount runs)."""
    return bool(os.path.exists("/dev/fuse") and ctypes.util.find_library("fuse3")
                and shutil.which("fusermount3"))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(can_mount(), "no FUSE here (needs /dev/fuse and libfuse3)")
class MountTest(unittest.TestCase):
    """mount.py: the providers as folders, read-write where the provider manages its files."""

    def test_mount(self):
        hub = agentbus.Hub()
        root, mnt = tempfile.mkdtemp(), tempfile.mkdtemp()
        with open(os.path.join(root, "a.txt"), "w") as f:
            f.write("hello")
        py = sys.executable
        procs = [subprocess.Popen([py, "provider.py", "dir", "docs", root], cwd=FILES),
                 subprocess.Popen([py, "provider.py", "bot", "bot:notes"], cwd=FILES)]
        mount = subprocess.Popen([py, "mount.py", mnt], cwd=FILES)
        try:
            deadline = time.monotonic() + 15
            while sorted(os.listdir(mnt)) != ["bot:notes", "docs"] and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertEqual(sorted(os.listdir(mnt)), ["bot:notes", "docs"])
            docs = os.path.join(mnt, "docs")
            self.assertEqual(read(os.path.join(docs, "a.txt")), "hello")

            with open(os.path.join(docs, "b.md"), "w") as f:
                f.write("one\n")
            with open(os.path.join(docs, "b.md"), "a") as f:
                f.write("two\n")
            self.assertEqual(read(os.path.join(root, "b.md")), "one\ntwo\n")
            os.mkdir(os.path.join(docs, "sub"))
            os.rename(os.path.join(docs, "b.md"), os.path.join(docs, "sub", "b.md"))
            self.assertEqual(sorted(os.listdir(docs)), ["a.txt", "sub"])
            self.assertEqual(read(os.path.join(root, "sub", "b.md")), "one\ntwo\n")
            with self.assertRaises(OSError):
                os.rmdir(os.path.join(docs, "sub"))
            os.remove(os.path.join(docs, "sub", "b.md"))
            os.rmdir(os.path.join(docs, "sub"))
            self.assertEqual(os.listdir(root), ["a.txt"])

            notes = os.path.join(mnt, "bot:notes")
            self.assertTrue(read(os.path.join(notes, "plans", "kv-store-design.md")).startswith("# KV"))
            with self.assertRaises(OSError) as e:
                open(os.path.join(notes, "x.txt"), "w")
            self.assertEqual(e.exception.errno, 30)  # EROFS
        finally:
            subprocess.run([py, "mount.py", "--unmount", mnt], cwd=FILES)
            mount.wait(10)
            for p in procs:
                p.terminate()
                p.wait()
            shutil.rmtree(root)
            os.rmdir(mnt)
            del hub


if __name__ == "__main__":
    unittest.main()
