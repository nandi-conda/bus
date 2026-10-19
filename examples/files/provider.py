"""A file proc: provides files.list and files.read on the hub.

    python provider.py dir NAME PATH     the files under PATH
    python provider.py bot NAME          files a bot wrote, answered a little later
"""

import os
import sys
import time

import agentbus

from common import FUNCTION, READ, READ_SCHEMA, SCHEMA, check_name, check_size, file_content, mime_of

BOT_FILES = {
    "drafts/release-notes-0.2.md": "# agentbus 0.2\n\n- File -> Open reads files from every proc.\n",
    "plans/kv-store-design.md": "# KV store\n\nOne proc per store; get/put as bus functions.\n",
    "reports/test-failures-2026-10-18.txt": "All 12 tests passed after the retry fix.\n",
}


def entry(name, uri, size, mtime):
    return {"name": name, "uri": uri, "size": size, "mtime": mtime, "mime": mime_of(name)}


def walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            st = os.stat(full)
            yield entry(os.path.relpath(full, root), "file://" + full, st.st_size, int(st.st_mtime))


def read_under(root, name):
    """The bytes of ``name`` under ``root``; a name reaching outside root is refused."""
    path = os.path.realpath(os.path.join(root, check_name(name)))
    if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
        raise ValueError(f"no file {name!r} here")
    check_size(name, os.path.getsize(path))
    with open(path, "rb") as f:
        return f.read()


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
        label, list_files, read = sys.argv[2], lambda: list(walk(root)), lambda n: read_under(root, n)
    elif len(sys.argv) >= 3 and sys.argv[1] == "bot":
        label, list_files, read = sys.argv[2], lambda: bot_files(sys.argv[2]), bot_read
    else:
        sys.exit(__doc__)

    procs = agentbus.Procs()

    @procs.function(FUNCTION, SCHEMA)
    def files_list(path=""):
        files = [f for f in list_files() if f["name"].startswith(path)]
        print(f"{label}: sent {len(files)} files", flush=True)
        return {"provider": label, "files": files}

    @procs.function(READ, READ_SCHEMA)
    def files_read(file):
        answer = file_content(file, read(file))
        print(f"{label}: sent {file} ({answer['size']} bytes)", flush=True)
        return answer

    print(f"{label}: providing {FUNCTION}, {READ}", flush=True)
    procs.run_forever()


if __name__ == "__main__":
    main()
