"""Router + agent + sender on one bus. Run inside dbus-run-session (pixi run demo does)."""

import subprocess
import sys
import time

py = sys.executable
router = subprocess.Popen([py, "examples/router.py"])
agent = subprocess.Popen([py, "examples/agent.py"])
try:
    time.sleep(1)
    sys.exit(subprocess.run([py, "examples/sender.py", *sys.argv[1:]]).returncode)
finally:
    for p in (agent, router):
        p.terminate()
        p.wait()
