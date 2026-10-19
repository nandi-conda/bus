"""A file proc: provides files.list, .read, .write, .delete, .mkdir, .rmdir and .rename on the hub.

    python provider.py dir NAME PATH     the files under PATH
    python provider.py bot NAME          files a bot wrote, answered a little later; kept in
                                         memory, so they last as long as the proc
"""

import sys
import threading
import time

import agentbus

from common import (DELETE, FUNCTION, MANAGE_SCHEMAS, MKDIR, READ, READ_SCHEMA, RENAME, RMDIR, SCHEMA,
                    WRITE, check_name, check_path, check_size, content_bytes, file_content, mime_of)
from dirstore import DirStore

BOT_FILES = {
    "drafts/release-notes-0.2.md": "# agentbus 0.2\n\n- File -> Open reads files from every proc.\n",
    "plans/kv-store-design.md": "# KV store\n\nOne proc per store; get/put as bus functions.\n",
    "reports/test-failures-2026-10-18.txt": "All 12 tests passed after the retry fix.\n",
}


class Bot:
    """A bot's files, kept in memory: name -> (bytes, mtime), plus its empty folders."""

    def __init__(self, label):
        now = int(time.time())
        self.label = label
        self.files = {f: (text.encode(), now) for f, text in BOT_FILES.items()}
        self.empty_dirs = set()
        self.lock = threading.Lock()

    def entry(self, name):
        data, mtime = self.files[name]
        return {"name": name, "uri": f"bot://{self.label}/{name}", "size": len(data), "mtime": mtime,
                "mime": mime_of(name)}

    def dirs(self):
        out = set(self.empty_dirs)
        for name in self.files:
            parts = name.split("/")[:-1]
            out.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
        return out

    def list(self, path=""):
        time.sleep(0.8)  # a bot answers a little later than the disk
        with self.lock:
            return ([self.entry(f) for f in sorted(self.files) if f.startswith(path)],
                    sorted(d for d in self.dirs() if d.startswith(path)))

    def read(self, name):
        with self.lock:
            if check_name(name) not in self.files:
                raise ValueError(f"no file {name!r} here")
            return self.files[name][0]

    def write(self, name, data):
        with self.lock:
            check_size(name, len(data))
            if check_path(name) in self.dirs():
                raise ValueError(f"{name!r} is a folder")
            self.files[name] = (data, int(time.time()))
            return self.entry(name)

    def delete(self, name):
        with self.lock:
            if self.files.pop(check_path(name), None) is None:
                raise ValueError(f"no file {name!r} here")

    def mkdir(self, name):
        with self.lock:
            if check_path(name) in self.files or name in self.dirs():
                raise ValueError(f"{name!r} already exists")
            self.empty_dirs.add(name)

    def rmdir(self, name):
        with self.lock:
            if check_path(name) not in self.dirs():
                raise ValueError(f"no folder {name!r} here")
            if any(n.startswith(name + "/") for n in (*self.files, *self.dirs())):
                raise ValueError(f"folder {name!r} is not empty")
            self.empty_dirs.discard(name)

    def rename(self, old, new):
        with self.lock:
            check_path(old), check_path(new)
            if new in self.dirs():
                raise ValueError(f"{new!r} is a folder")
            if old in self.files:
                self.files[new] = self.files.pop(old)
                return
            if old not in self.dirs():
                raise ValueError(f"no file {old!r} here")
            if (new + "/").startswith(old + "/"):
                raise ValueError(f"can't move {old!r} into itself")
            if new in self.files:
                raise ValueError(f"{new!r} is a file")
            moved = lambda n: new + n[len(old):] if n == old or n.startswith(old + "/") else n
            self.files = {moved(n): v for n, v in self.files.items()}
            self.empty_dirs = {moved(d) for d in self.empty_dirs}


def serve(procs, label, store):
    """The file functions on ``store`` (a DirStore or a Bot)."""

    def done(name, what):
        print(f"{label}: {what} {name}", flush=True)
        return {"name": name}

    @procs.function(FUNCTION, SCHEMA)
    def files_list(path=""):
        files, dirs = store.list(path)
        print(f"{label}: sent {len(files)} files", flush=True)
        return {"provider": label, "files": files, "dirs": dirs}

    @procs.function(READ, READ_SCHEMA)
    def files_read(file):
        answer = file_content(file, store.read(file))
        print(f"{label}: sent {file} ({answer['size']} bytes)", flush=True)
        return answer

    @procs.function(WRITE, MANAGE_SCHEMAS[WRITE])
    def files_write(file, encoding, content):
        data = content_bytes({"encoding": encoding, "content": content})
        f = store.write(file, data)
        print(f"{label}: wrote {file} ({len(data)} bytes)", flush=True)
        return f

    @procs.function(DELETE, MANAGE_SCHEMAS[DELETE])
    def files_delete(file):
        store.delete(file)
        return done(file, "deleted")

    @procs.function(MKDIR, MANAGE_SCHEMAS[MKDIR])
    def files_mkdir(dir):
        store.mkdir(dir)
        return done(dir, "made folder")

    @procs.function(RMDIR, MANAGE_SCHEMAS[RMDIR])
    def files_rmdir(dir):
        store.rmdir(dir)
        return done(dir, "removed folder")

    @procs.function(RENAME, MANAGE_SCHEMAS[RENAME])
    def files_rename(file, to):
        store.rename(file, to)
        return done(to, f"renamed {file} to")


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "dir":
        label, store = sys.argv[2], DirStore(sys.argv[3])
    elif len(sys.argv) >= 3 and sys.argv[1] == "bot":
        label, store = sys.argv[2], Bot(sys.argv[2])
    else:
        sys.exit(__doc__)
    procs = agentbus.Procs()
    serve(procs, label, store)
    print(f"{label}: providing {', '.join(procs._provides)}", flush=True)
    procs.run_forever()


if __name__ == "__main__":
    main()
