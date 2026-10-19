"""Hub + weather proc (local only, no rookery identity) + app on one bus. Run inside
dbus-run-session (pixi run demo does)."""

import os
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))


def start(script, *args):
    return subprocess.Popen([sys.executable, os.path.join(here, script), *args])


os.environ["AGENTBUS_SERVICES"] = ""  # local only: no relay, no rookery signup
hub = start("hub.py")
time.sleep(0.5)
weather = start("weather.py", "--local")
time.sleep(0.5)
try:
    sys.exit(subprocess.run([sys.executable, os.path.join(here, "app.py"), *sys.argv[1:]]).returncode)
finally:
    for p in (weather, hub):
        p.terminate()
        p.wait()
