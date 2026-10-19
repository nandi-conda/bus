"""File -> Open demo on one bus: the hub, a directory proc, a bot proc, and the GUI.
Run inside dbus-run-session (pixi run -e gui files-demo does)."""

import os
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
py = sys.executable
hub = subprocess.Popen([py, "../hub.py"], cwd=here)
time.sleep(0.5)
procs = [
    hub,
    subprocess.Popen([py, "provider.py", "dir", "local:crates", "../../crates"], cwd=here),
    subprocess.Popen([py, "provider.py", "bot", "bot:notes"], cwd=here),
]
try:
    sys.exit(subprocess.run([py, "gui.py", *sys.argv[1:]], cwd=here).returncode)
finally:
    for p in reversed(procs):
        p.terminate()
        p.wait()
