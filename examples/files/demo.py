"""File -> Open demo on one bus: a directory provider, a bot provider, and the GUI.
Run inside dbus-run-session (pixi run -e gui files-demo does)."""

import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
py = sys.executable
providers = [
    subprocess.Popen([py, "provider.py", "dir", "local:crates", "../../crates"], cwd=here),
    subprocess.Popen([py, "provider.py", "bot", "bot:notes"], cwd=here),
]
try:
    sys.exit(subprocess.run([py, "gui.py", *sys.argv[1:]], cwd=here).returncode)
finally:
    for p in providers:
        p.terminate()
        p.wait()
