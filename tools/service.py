"""Install the hub as a systemd user service.

    pixi run install-service     # write the unit, enable and start the hub
    pixi run uninstall-service   # stop, disable and remove it

ExecStart is this pixi env's python, resolved now, so nothing builds at runtime. A D-Bus
activation file names the unit, so the first call to the hub also starts it."""

import os
import subprocess
import sys
from pathlib import Path

import agentbus

ROOT = Path(__file__).resolve().parent.parent
UNIT = "agentbus-hub.service"


def xdg(var, default):
    return Path(os.environ.get(var) or Path.home() / default)


def files(python=sys.executable, bus_name=agentbus.HUB_NAME):
    """{path: content} for the systemd unit and the D-Bus activation file."""
    return {
        xdg("XDG_CONFIG_HOME", ".config") / "systemd/user" / UNIT: f"""[Unit]
Description=agentbus hub ({bus_name})

[Service]
Type=dbus
BusName={bus_name}
WorkingDirectory={ROOT}
ExecStart={python} -u {ROOT}/examples/hub.py
Restart=on-failure

[Install]
WantedBy=default.target
""",
        xdg("XDG_DATA_HOME", ".local/share") / "dbus-1/services" / f"{bus_name}.service": f"""[D-BUS Service]
Name={bus_name}
Exec=/bin/false
SystemdService={UNIT}
""",
    }


def systemctl(*args, check=True):
    subprocess.run(["systemctl", "--user", *args], check=check)


def install():
    for path, content in files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"wrote {path}")
    systemctl("daemon-reload")
    systemctl("enable", "--now", UNIT)
    print(f"{UNIT} enabled and started")


def uninstall():
    systemctl("disable", "--now", UNIT, check=False)
    for path in files():
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    systemctl("daemon-reload")


if __name__ == "__main__":
    {"install": install, "uninstall": uninstall}[sys.argv[1]]()
