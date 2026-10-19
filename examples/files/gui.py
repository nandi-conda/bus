"""File -> Open: ask every files.list provider on the hub and list each answer as it arrives."""

import sys
import threading

import agentbus
from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel, QMainWindow,
                               QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from common import FUNCTION


class Replies(QObject):
    """Carries answers from the calling threads to the GUI thread."""

    arrived = Signal(dict)
    failed = Signal(str)


def human_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class OpenDialog(QDialog):
    """Lists the files every files.list provider answers with."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Open File")
        self.resize(620, 400)
        self.status = QLabel("Asking the bus for files…")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Provider", "Size"])
        self.tree.setColumnWidth(0, 330)
        self.tree.setColumnWidth(1, 130)
        self.tree.itemDoubleClicked.connect(lambda item: item.parent() and self.accept())
        buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        for w in (self.status, self.tree, buttons):
            layout.addWidget(w)

        self.replies = Replies()
        self.replies.arrived.connect(self.on_files)
        self.replies.failed.connect(self.on_failed)
        self.procs = parent.procs
        providers = [owner for owner, _ in self.procs.providers(FUNCTION)]
        if not providers:
            self.status.setText(f"Nobody on the bus provides {FUNCTION}")
        self.waiting, self.files = len(providers), 0
        for owner in providers:
            threading.Thread(target=self.ask, args=(owner,), daemon=True).start()

    def ask(self, owner):
        """One call per provider, so a slow remote proc never holds up the local disk."""
        try:
            self.replies.arrived.emit(self.procs.call(FUNCTION, timeout=10, provider=owner))
        except (agentbus.CallError, agentbus.NoProvider, TimeoutError) as e:
            self.replies.failed.emit(f"{owner}: {e}")

    @Slot(dict)
    def on_files(self, answer):
        """One collapsible group per provider, its files under it."""
        group = QTreeWidgetItem([f"{answer['provider']} ({len(answer['files'])})"])
        group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
        for f in answer["files"]:
            item = QTreeWidgetItem(group, [f["name"], answer["provider"], human_size(f.get("size"))])
            item.setToolTip(0, f["uri"])
            item.setData(0, Qt.UserRole, f["uri"])
        self.tree.addTopLevelItem(group)
        group.setFirstColumnSpanned(True)
        group.setExpanded(len(answer["files"]) <= 10)
        self.files += len(answer["files"])
        self.answered()

    @Slot(str)
    def on_failed(self, message):
        print(f"files.list failed: {message}", file=sys.stderr, flush=True)
        self.answered()

    def answered(self):
        self.waiting -= 1
        more = f", waiting on {self.waiting}" if self.waiting else ""
        self.status.setText(f"{self.files} files from {self.tree.topLevelItemCount()} providers{more}")

    def selected(self):
        item = self.tree.currentItem()
        return (item.text(1), item.data(0, Qt.UserRole)) if item and item.parent() else None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bus Editor")
        self.resize(720, 480)
        self.procs = agentbus.Procs()
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("File → Open lists files from everyone on the bus.")
        self.setCentralWidget(self.editor)
        open_action = QAction("&Open…", self, shortcut=QKeySequence.Open, triggered=self.open_file)
        quit_action = QAction("&Quit", self, shortcut=QKeySequence.Quit, triggered=self.close)
        menu = self.menuBar().addMenu("&File")
        menu.addAction(open_action)
        menu.addAction(quit_action)

    def open_file(self):
        dialog = OpenDialog(self)
        if dialog.exec() and (choice := dialog.selected()):
            provider, uri = choice
            self.statusBar().showMessage(f"Chose {uri} from {provider}")


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
