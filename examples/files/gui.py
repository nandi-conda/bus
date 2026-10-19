"""File -> Open: ask every files.list provider on the hub and list each answer as it arrives, then
read the chosen file with files.read from the provider that listed it and show it in the window:
text in the editor, images in an image view.

The hub is the installed one (`pixi run install-service`; D-Bus starts it on the first call) or
any hub on this session bus. A hub that just started is still finding remote providers through
the relay, so the dialog keeps looking for WAIT seconds and asks each provider as it appears.
Problems (no hub, a provider that fails) show in the dialog itself."""

import sys
import threading
import time

import agentbus
from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QAction, QBrush, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel, QMainWindow,
                               QPlainTextEdit, QScrollArea, QStackedWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

from common import FUNCTION, READ, content_bytes

WAIT = 30  # seconds to keep looking for providers, as examples/app.py does
CALL_TIMEOUT = 90  # past a remote proc's cold start on Modal, as examples/app.py allows


class Replies(QObject):
    """Carries news from the finder and calling threads to the GUI thread."""

    asking = Signal(str)  # a provider was found and asked
    arrived = Signal(str, dict)  # owner, answer
    failed = Signal(str, str)  # owner, error
    hub_missing = Signal(str)
    done_looking = Signal()


class Reads(QObject):
    """Carries a files.read answer (or its failure) to the GUI thread."""

    read = Signal(object, object)  # the chosen file, answer (as objects: no QVariant round trip)
    failed = Signal(object, str)  # the chosen file, error


def human_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def error_text(e):
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


class OpenDialog(QDialog):
    """Lists the files every files.list provider answers with."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Open File")
        self.resize(640, 420)
        self.procs = parent.procs
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Provider", "Size"])
        self.tree.setColumnWidth(0, 340)
        self.tree.setColumnWidth(1, 140)
        self.tree.itemDoubleClicked.connect(lambda item: self.selected() and self.accept())
        self.tree.currentItemChanged.connect(self.update_buttons)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Retry
                                        | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.Retry).clicked.connect(self.look)
        layout = QVBoxLayout(self)
        for w in (self.status, self.tree, self.buttons):
            layout.addWidget(w)
        self.stop = threading.Event()
        self.finished.connect(lambda _: self.stop.set())
        self.look()

    def look(self):
        """(Re)start: clear the list and look for providers in the background."""
        self.stop.set()
        self.stop = threading.Event()
        self.tree.clear()
        self.asked, self.answered, self.files, self.looking, self.problem = 0, 0, 0, True, None
        self.replies = Replies()
        self.replies.asking.connect(self.on_asking)
        self.replies.arrived.connect(self.on_files)
        self.replies.failed.connect(self.on_failed)
        self.replies.hub_missing.connect(self.on_hub_missing)
        self.replies.done_looking.connect(self.on_done_looking)
        threading.Thread(target=self.find, args=(self.replies, self.stop), daemon=True).start()
        self.update_status()

    def find(self, replies, stop):
        """Ask each files.list provider once, as the hub learns of it, until WAIT runs out."""
        seen = set()
        deadline = time.monotonic() + WAIT
        while not stop.is_set():
            try:
                owners = [owner for owner, _ in self.procs.providers(FUNCTION)]
            except agentbus.HubNotRunning as e:
                return replies.hub_missing.emit(str(e))
            except agentbus.BusError as e:
                return replies.hub_missing.emit(error_text(e))
            for owner in owners:
                if owner not in seen:
                    seen.add(owner)
                    replies.asking.emit(owner)
                    threading.Thread(target=self.ask, args=(replies, stop, owner), daemon=True).start()
            if time.monotonic() >= deadline or stop.wait(0.5):
                break
        if not stop.is_set():
            replies.done_looking.emit()

    def ask(self, replies, stop, owner):
        """One call per provider, so a slow remote proc never holds up the local disk."""
        try:
            answer = self.procs.call(FUNCTION, timeout=CALL_TIMEOUT, provider=owner)
        except Exception as e:  # shown in the list: every way a call can fail is the user's to see
            answer = e
        if stop.is_set():
            return
        if isinstance(answer, Exception):
            replies.failed.emit(owner, error_text(answer))
        else:
            replies.arrived.emit(owner, answer)

    @Slot(str)
    def on_asking(self, owner):
        self.asked += 1
        self.update_status()

    @Slot(str, dict)
    def on_files(self, owner, answer):
        """One collapsible group per provider, its files under it."""
        group = self.group(f"{answer['provider']} ({len(answer['files'])})", owner)
        for f in answer["files"]:
            item = QTreeWidgetItem(group, [f["name"], answer["provider"], human_size(f.get("size"))])
            item.setToolTip(0, f["uri"])
            item.setData(0, Qt.UserRole, {"owner": owner, "provider": answer["provider"],
                                          "name": f["name"], "uri": f["uri"]})
        group.setExpanded(len(answer["files"]) <= 10)
        self.files += len(answer["files"])
        self.answered += 1
        self.update_status()

    @Slot(str, str)
    def on_failed(self, owner, message):
        """A provider that failed gets a row saying why, where its files would have been."""
        group = self.group(f"{owner} couldn't list files: {message}", owner)
        group.setForeground(0, QBrush(Qt.red))
        self.answered += 1
        self.update_status()

    @Slot(str)
    def on_hub_missing(self, message):
        self.looking, self.problem = False, message
        self.update_status()

    @Slot()
    def on_done_looking(self):
        self.looking = False
        self.update_status()

    def group(self, text, owner):
        group = QTreeWidgetItem([text])
        group.setToolTip(0, owner)
        group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
        self.tree.addTopLevelItem(group)
        group.setFirstColumnSpanned(True)
        return group

    def update_status(self):
        if self.problem:
            text = f"Can't reach the bus. {self.problem}"
        elif not self.asked:
            text = (f"Looking for {FUNCTION} providers…" if self.looking else
                    f"Nobody on the bus provides {FUNCTION} yet. Start a file proc, or Retry.")
        else:
            text = f"{self.files} files from {self.answered} of {self.asked} providers"
            if self.asked > self.answered:
                text += f", waiting on {self.asked - self.answered}"
            if self.looking:
                text += ", still looking for more…"
        self.status.setText(text)
        self.status.setStyleSheet("color: red;" if self.problem else "")
        self.update_buttons()

    def update_buttons(self):
        self.buttons.button(QDialogButtonBox.Open).setEnabled(self.selected() is not None)
        self.buttons.button(QDialogButtonBox.Retry).setVisible(
            bool(self.problem) or (not self.looking and not self.asked))

    def selected(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item and item.parent() else None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(720, 480)
        self.procs = agentbus.Procs()
        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)  # no files.write yet, so nothing to save edits to
        self.editor.setPlaceholderText("File → Open lists files from every proc on the bus.")
        self.image = QLabel(alignment=Qt.AlignCenter)
        image_view = QScrollArea()
        image_view.setWidget(self.image)
        image_view.setWidgetResizable(True)
        self.views = QStackedWidget()
        self.views.addWidget(self.editor)
        self.views.addWidget(image_view)
        self.setCentralWidget(self.views)
        self.reads = Reads()
        self.reads.read.connect(self.show_file)
        self.reads.failed.connect(self.read_failed)
        self.opening = None  # the file being read; a later Open supersedes it
        self.set_title(None)
        open_action = QAction("&Open…", self, shortcut=QKeySequence.Open, triggered=self.open_file)
        quit_action = QAction("&Quit", self, shortcut=QKeySequence.Quit, triggered=self.close)
        menu = self.menuBar().addMenu("&File")
        menu.addAction(open_action)
        menu.addAction(quit_action)

    def set_title(self, chosen):
        self.setWindowTitle(f"{chosen['name']} ({chosen['provider']}) — Bus Editor" if chosen
                            else "Bus Editor")

    def open_file(self):
        dialog = OpenDialog(self)
        if dialog.exec() and (chosen := dialog.selected()):
            self.read(chosen)

    def read(self, chosen):
        """Ask the provider that listed the file for its contents, off the GUI thread."""
        self.opening = chosen
        self.statusBar().showMessage(f"Opening {chosen['name']} from {chosen['provider']}…")
        threading.Thread(target=self.fetch, args=(chosen,), daemon=True).start()

    def fetch(self, chosen):
        try:
            answer = self.procs.call(READ, timeout=CALL_TIMEOUT, provider=chosen["owner"],
                                     file=chosen["name"])
        except Exception as e:  # shown in the status bar: the user picked this file
            return self.reads.failed.emit(chosen, error_text(e))
        self.reads.read.emit(chosen, answer)

    @Slot(object, object)
    def show_file(self, chosen, answer):
        if chosen is not self.opening:
            return
        data = content_bytes(answer)
        image = QImage.fromData(data) if answer["mime"].startswith("image/") else QImage()
        if not image.isNull():
            self.image.setPixmap(QPixmap.fromImage(image))
            self.views.setCurrentIndex(1)
        elif answer["encoding"] == "utf-8":
            self.editor.setPlainText(answer["content"])
            self.views.setCurrentIndex(0)
        else:
            self.statusBar().showMessage(
                f"Can't show {chosen['name']}: {answer['mime']}, {human_size(answer['size'])} of binary data")
            return
        self.set_title(chosen)
        self.statusBar().showMessage(f"{chosen['uri']} · {answer['mime']} · {human_size(answer['size'])}")

    @Slot(object, str)
    def read_failed(self, chosen, message):
        if chosen is self.opening:
            self.statusBar().showMessage(f"Couldn't open {chosen['name']} from {chosen['provider']}: {message}")


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
