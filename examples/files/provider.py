"""A file proc: provides files.list and files.read on the hub.

    python provider.py dir NAME PATH     the files under PATH, which it also manages (files.write,
                                         .delete, .mkdir, .rmdir, .rename)
    python provider.py bot NAME          files a bot wrote, answered a little later; it manages
                                         them too, in memory, so they last as long as the proc
"""

import os
import sys
import threading
import time

import agentbus

from common import (DELETE, FUNCTION, MANAGE_SCHEMAS, MKDIR, READ, READ_SCHEMA, RENAME, RMDIR, SCHEMA,
                    WRITE, check_name, check_path, check_size, content_bytes, file_content, mime_of)

BOT_FILES = {
    "drafts/release-notes-0.2.md": "# agentbus 0.2\n\n- File -> Open reads files from every proc.\n",
    "plans/kv-store-design.md": "# KV store\n\nOne proc per store; get/put as bus functions.\n",
    "reports/test-failures-2026-10-18.txt": "All 12 tests passed after the retry fix.\n",
}


def entry(name, uri, size, mtime):
    return {"name": name, "uri": uri, "size": size, "mtime": mtime, "mime": mime_of(name)}


def file_entry(root, full):
    st = os.stat(full)
    return entry(os.path.relpath(full, root), "file://" + full, st.st_size, int(st.st_mtime))


def walk(root):
    """(files, dirs) under root, skipping hidden folders and __pycache__."""
    files, dirs = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        dirs.extend(os.path.relpath(os.path.join(dirpath, d), root) for d in dirnames)
        files.extend(file_entry(root, os.path.join(dirpath, f)) for f in sorted(filenames))
    return files, dirs


def under(root, name):
    """The real path of ``name`` under ``root``; a name reaching outside root (through ``..`` or a
    symlink) is refused."""
    path = os.path.realpath(os.path.join(root, check_name(name)))
    if path == root or os.path.commonpath([root, path]) != root:
        raise ValueError(f"no file {name!r} here")
    return path


def read_under(root, name):
    """The bytes of ``name`` under ``root``."""
    path = under(root, name)
    if not os.path.isfile(path):
        raise ValueError(f"no file {name!r} here")
    check_size(name, os.path.getsize(path))
    with open(path, "rb") as f:
        return f.read()


def manage(procs, label, root):
    """files.write, .delete, .mkdir, .rmdir and .rename on the files under root."""

    def target(name):
        """Where a new or changed ``name`` goes: its parent must resolve inside root."""
        check_path(name)
        parent = under(root, os.path.dirname(name)) if "/" in name else root
        return os.path.join(parent, os.path.basename(name))

    def done(name, what):
        print(f"{label}: {what} {name}", flush=True)
        return {"name": name}

    @procs.function(WRITE, MANAGE_SCHEMAS[WRITE])
    def files_write(file, encoding, content):
        data = content_bytes({"encoding": encoding, "content": content})
        check_size(file, len(data))
        path = target(file)
        if os.path.isdir(path):
            raise ValueError(f"{file!r} is a folder")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.agentbus-{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        print(f"{label}: wrote {file} ({len(data)} bytes)", flush=True)
        return file_entry(root, path)

    @procs.function(DELETE, MANAGE_SCHEMAS[DELETE])
    def files_delete(file):
        path = target(file)
        if not os.path.isfile(path) or os.path.islink(path):
            raise ValueError(f"no file {file!r} here")
        os.remove(path)
        return done(file, "deleted")

    @procs.function(MKDIR, MANAGE_SCHEMAS[MKDIR])
    def files_mkdir(dir):
        path = target(dir)
        if os.path.lexists(path):
            raise ValueError(f"{dir!r} already exists")
        os.makedirs(path)
        return done(dir, "made folder")

    @procs.function(RMDIR, MANAGE_SCHEMAS[RMDIR])
    def files_rmdir(dir):
        path = target(dir)
        if not os.path.isdir(path) or os.path.islink(path):
            raise ValueError(f"no folder {dir!r} here")
        if os.listdir(path):
            raise ValueError(f"folder {dir!r} is not empty")
        os.rmdir(path)
        return done(dir, "removed folder")

    @procs.function(RENAME, MANAGE_SCHEMAS[RENAME])
    def files_rename(file, to):
        src, dst = target(file), target(to)
        if not os.path.lexists(src):
            raise ValueError(f"no file {file!r} here")
        if os.path.isdir(dst):
            raise ValueError(f"{to!r} is a folder")
        if os.path.isdir(src) and os.path.commonpath([src, dst]) == src:
            raise ValueError(f"can't move {file!r} into itself")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
        return done(to, f"renamed {file} to")


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
        return entry(name, f"bot://{self.label}/{name}", len(data), mtime)

    def dirs(self):
        out = set(self.empty_dirs)
        for name in self.files:
            parts = name.split("/")[:-1]
            out.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
        return out

    def list(self):
        time.sleep(0.8)  # a bot answers a little later than the disk
        with self.lock:
            return [self.entry(f) for f in sorted(self.files)], sorted(self.dirs())

    def read(self, name):
        with self.lock:
            if check_name(name) not in self.files:
                raise ValueError(f"no file {name!r} here")
            return self.files[name][0]

    def write(self, name, data):
        with self.lock:
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


def manage_bot(procs, bot):
    """files.write, .delete, .mkdir, .rmdir and .rename on a bot's in-memory files."""

    def done(name, what):
        print(f"{bot.label}: {what} {name}", flush=True)
        return {"name": name}

    @procs.function(WRITE, MANAGE_SCHEMAS[WRITE])
    def files_write(file, encoding, content):
        data = content_bytes({"encoding": encoding, "content": content})
        check_size(file, len(data))
        print(f"{bot.label}: wrote {file} ({len(data)} bytes)", flush=True)
        return bot.write(file, data)

    @procs.function(DELETE, MANAGE_SCHEMAS[DELETE])
    def files_delete(file):
        bot.delete(file)
        return done(file, "deleted")

    @procs.function(MKDIR, MANAGE_SCHEMAS[MKDIR])
    def files_mkdir(dir):
        bot.mkdir(dir)
        return done(dir, "made folder")

    @procs.function(RMDIR, MANAGE_SCHEMAS[RMDIR])
    def files_rmdir(dir):
        bot.rmdir(dir)
        return done(dir, "removed folder")

    @procs.function(RENAME, MANAGE_SCHEMAS[RENAME])
    def files_rename(file, to):
        bot.rename(file, to)
        return done(to, f"renamed {file} to")


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "dir":
        root = os.path.realpath(sys.argv[3])
        label, list_files, read = sys.argv[2], lambda: walk(root), lambda n: read_under(root, n)
    elif len(sys.argv) >= 3 and sys.argv[1] == "bot":
        root, bot = None, Bot(sys.argv[2])
        label, list_files, read = sys.argv[2], bot.list, bot.read
    else:
        sys.exit(__doc__)

    procs = agentbus.Procs()

    @procs.function(FUNCTION, SCHEMA)
    def files_list(path=""):
        files, dirs = list_files()
        files = [f for f in files if f["name"].startswith(path)]
        print(f"{label}: sent {len(files)} files", flush=True)
        return {"provider": label, "files": files, "dirs": [d for d in dirs if d.startswith(path)]}

    @procs.function(READ, READ_SCHEMA)
    def files_read(file):
        answer = file_content(file, read(file))
        print(f"{label}: sent {file} ({answer['size']} bytes)", flush=True)
        return answer

    if root:
        manage(procs, label, root)
    else:
        manage_bot(procs, bot)
    print(f"{label}: providing {', '.join(procs._provides)}", flush=True)
    procs.run_forever()


if __name__ == "__main__":
    main()
