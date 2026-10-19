"""File -> Open: broadcast GetFiles on the bus and list every SendFiles reply as it arrives."""

import sys
import uuid

from PySide6.QtCore import Slot
from PySide6.QtDBus import QDBusConnection, QDBusMessage
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel, QMainWindow,
                               QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from common import IFACE, PATH


class OpenDialog(QDialog):
    """Lists files from whichever providers answer this dialog's request."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Open File")
        self.resize(560, 380)
        self.request = uuid.uuid4().hex
        self.status = QLabel("Asking the bus for files…")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Provider"])
        self.tree.setColumnWidth(0, 360)
        self.tree.itemDoubleClicked.connect(self.accept)
        buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        for w in (self.status, self.tree, buttons):
            layout.addWidget(w)

        self.bus = QDBusConnection.sessionBus()
        # The leading "1" is the SLOT() prefix Qt expects; PySide does not add it.
        if not self.bus.connect("", PATH, IFACE, "SendFiles", self, "1on_send_files(QDBusMessage)"):
            self.status.setText(f"Cannot subscribe to SendFiles: {self.bus.lastError().message()}")
            return
        get_files = QDBusMessage.createSignal(PATH, IFACE, "GetFiles")
        get_files.setArguments([self.request])
        self.bus.send(get_files)

    @Slot(QDBusMessage)
    def on_send_files(self, msg):
        request, provider, files = msg.arguments()
        if request != self.request:
            return
        for f in files:
            item = QTreeWidgetItem([f, provider])
            self.tree.addTopLevelItem(item)
        providers = {self.tree.topLevelItem(i).text(1) for i in range(self.tree.topLevelItemCount())}
        self.status.setText(f"{self.tree.topLevelItemCount()} files from {len(providers)} providers")

    def done(self, result):
        self.bus.disconnect("", PATH, IFACE, "SendFiles", self, "1on_send_files(QDBusMessage)")
        super().done(result)

    def selected(self):
        item = self.tree.currentItem()
        return (item.text(1), item.text(0)) if item else None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bus Editor")
        self.resize(720, 480)
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
            provider, path = choice
            self.statusBar().showMessage(f"Chose {path} from {provider}")


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
