"""A file proc: provides files.list and files.read on the hub.

    python provider.py dir NAME PATH     the files under PATH, which it also manages (files.write,
                                         .delete, .mkdir, .rmdir, .rename)
    python provider.py bot NAME          files a bot wrote, answered a little later (read-only)
"""

import os
import sys
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


def bot_files(name):
    now = int(time.time())
    time.sleep(0.8)
    return [entry(f, f"bot://{name}/{f}", len(text.encode()), now) for f, text in BOT_FILES.items()]


def bot_read(name):
    if check_name(name) not in BOT_FILES:
        raise ValueError(f"no file {name!r} here")
    return BOT_FILES[name].encode()


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "dir":
        root = os.path.realpath(sys.argv[3])
        label, list_files, read = sys.argv[2], lambda: walk(root), lambda n: read_under(root, n)
    elif len(sys.argv) >= 3 and sys.argv[1] == "bot":
        root = None
        label, list_files, read = sys.argv[2], lambda: (bot_files(sys.argv[2]), []), bot_read
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
    print(f"{label}: providing {', '.join(procs._provides)}", flush=True)
    procs.run_forever()


if __name__ == "__main__":
    main()
