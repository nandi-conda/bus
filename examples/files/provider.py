"""A file proc: provides files.list on the hub.

    python provider.py dir NAME PATH     the files under PATH
    python provider.py bot NAME          files a bot wrote, answered a little later
    python provider.py bots NAME         the same bot as its own proc in #bots on freeq
                                         (NAME is its nick; AGENTBUS_FREEQ_SERVER picks the server)
"""

import mimetypes
import os
import sys
import time

import agentbus

from common import FUNCTION, SCHEMA

BOT_FILES = [
    ("drafts/release-notes-0.2.md", 2140),
    ("plans/kv-store-design.md", 5310),
    ("reports/test-failures-2026-10-18.txt", 880),
]


def entry(name, uri, size, mtime):
    return {"name": name, "uri": uri, "size": size, "mtime": mtime,
            "mime": mimetypes.guess_type(name)[0] or "application/octet-stream"}


def walk(root):
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            st = os.stat(full)
            yield entry(os.path.relpath(full, root), "file://" + full, st.st_size, int(st.st_mtime))


def bot_files(name):
    now = int(time.time())
    time.sleep(0.8)
    return [entry(f, f"bot://{name}/{f}", size, now) for f, size in BOT_FILES]


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "dir":
        label, list_files = sys.argv[2], lambda: list(walk(sys.argv[3]))
    elif len(sys.argv) >= 3 and sys.argv[1] in ("bot", "bots"):
        label, list_files = sys.argv[2], lambda: bot_files(sys.argv[2])
    else:
        sys.exit(__doc__)

    if sys.argv[1] == "bots":
        # Answers travel back over freeq in one tag (~7 KB), so a #bots proc keeps its list short.
        label = f"#bots/{label}"
        procs = agentbus.Procs(name=sys.argv[2], description="files.list demo proc", freeq=True)
    else:
        procs = agentbus.Procs()

    @procs.function(FUNCTION, SCHEMA)
    def files_list(path=""):
        files = [f for f in list_files() if f["name"].startswith(path)]
        print(f"{label}: sent {len(files)} files", flush=True)
        return {"provider": label, "files": files}

    print(f"{label}: providing {FUNCTION}", flush=True)
    procs.run_forever()


if __name__ == "__main__":
    main()
