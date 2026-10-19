"""File -> Open demo: the hub, a directory proc, a bot proc, and the GUI on one bus.

Run inside dbus-run-session (pixi run -e gui files-demo does)."""

import os
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
py = sys.executable


def start(*args):
    return subprocess.Popen([py, *args], cwd=here)


def main():
    procs = [start("../hub.py")]
    time.sleep(0.5)
    procs += [start("provider.py", "dir", "local:crates", "../../crates"),
              start("provider.py", "bot", "bot:notes")]
    try:
        sys.exit(subprocess.run([py, "gui.py", *sys.argv[1:]], cwd=here).returncode)
    finally:
        for p in reversed(procs):
            p.terminate()
            p.wait()


if __name__ == "__main__":
    main()
