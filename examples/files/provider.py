"""A file provider: answers every GetFiles broadcast with a SendFiles broadcast.

    python provider.py dir NAME PATH     list the files under PATH
    python provider.py agent NAME        an agent offering files it wrote, after "thinking"
"""

import os
import sys

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Slot
from PySide6.QtDBus import QDBusConnection, QDBusMessage

from common import IFACE, PATH

AGENT_FILES = [
    "drafts/release-notes-0.2.md",
    "plans/kv-store-design.md",
    "reports/test-failures-2026-10-18.txt",
]


class Provider(QObject):
    def __init__(self, name, list_files, delay_ms=0):
        super().__init__()
        self.name, self.list_files, self.delay_ms = name, list_files, delay_ms
        self.bus = QDBusConnection.sessionBus()
        # The leading "1" is the SLOT() prefix Qt expects; PySide does not add it.
        if not self.bus.connect("", PATH, IFACE, "GetFiles", self, "1on_get_files(QDBusMessage)"):
            sys.exit(f"{name}: cannot subscribe to GetFiles: {self.bus.lastError().message()}")
        print(f"{name}: listening for GetFiles", flush=True)

    @Slot(QDBusMessage)
    def on_get_files(self, msg):
        request = msg.arguments()[0]
        QTimer.singleShot(self.delay_ms, lambda: self.send(request))

    def send(self, request):
        files = self.list_files()
        reply = QDBusMessage.createSignal(PATH, IFACE, "SendFiles")
        reply.setArguments([request, self.name, files])
        self.bus.send(reply)
        print(f"{self.name}: sent {len(files)} files for {request}", flush=True)


def walk(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        out += [os.path.relpath(os.path.join(dirpath, f), root) for f in sorted(filenames)]
    return out


def main():
    app = QCoreApplication(sys.argv)
    kind, name = sys.argv[1], sys.argv[2]
    if kind == "dir":
        provider = Provider(name, lambda: walk(sys.argv[3]))
    elif kind == "agent":
        provider = Provider(name, lambda: list(AGENT_FILES), delay_ms=800)
    else:
        sys.exit(__doc__)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
