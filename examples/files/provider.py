"""A file provider: answers every GetFiles broadcast with a SendFiles broadcast.

    python provider.py dir NAME PATH     list the files under PATH
    python provider.py bot NAME          a bot proc offering files it wrote, a little later
"""

import os
import sys
import threading

import agentbus

from common import IFACE, PATH

BOT_FILES = [
    "drafts/release-notes-0.2.md",
    "plans/kv-store-design.md",
    "reports/test-failures-2026-10-18.txt",
]


def walk(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        out += [os.path.relpath(os.path.join(dirpath, f), root) for f in sorted(filenames)]
    return out


def serve(name, list_files, delay=0.0):
    bus = agentbus.Signals()
    requests = bus.listen(PATH, IFACE, "GetFiles")
    print(f"{name}: listening for GetFiles", flush=True)

    def send(request):
        files = list_files()
        bus.broadcast(PATH, IFACE, "SendFiles", request, name, files)
        print(f"{name}: sent {len(files)} files for {request}", flush=True)

    for signal in requests:
        threading.Timer(delay, send, args=(signal["args"][0],)).start()


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "dir":
        serve(sys.argv[2], lambda: walk(sys.argv[3]))
    elif len(sys.argv) >= 3 and sys.argv[1] == "bot":
        serve(sys.argv[2], lambda: list(BOT_FILES), delay=0.8)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
