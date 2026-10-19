"""Install the hub, and the file mount, as systemd user services.

    pixi run install-service     # write the unit, enable and (re)start the hub
    pixi run uninstall-service   # stop, disable and remove it
    pixi run install-mount       # ~/agentbus: every file provider as a folder, from login on
    pixi run uninstall-mount

ExecStart is this pixi env's python, resolved now, so nothing builds at runtime. A D-Bus
activation file names the hub's unit, so the first call to the hub also starts it."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import agentbus

ROOT = Path(__file__).resolve().parent.parent
UNIT = "agentbus-hub.service"
MOUNT_UNIT = "agentbus-mount.service"


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


def mount_files(python=sys.executable):
    """{path: content} for the mount's systemd unit."""
    fusermount = shutil.which("fusermount3")
    if not fusermount:
        sys.exit("install-mount: no fusermount3 on PATH (Arch: pacman -S fuse3)")
    return {
        xdg("XDG_CONFIG_HOME", ".config") / "systemd/user" / MOUNT_UNIT: f"""[Unit]
Description=agentbus file providers at ~/agentbus
After={UNIT}
Wants={UNIT}

[Service]
WorkingDirectory={ROOT}/examples/files
ExecStart={python} -u {ROOT}/examples/files/mount.py %h/agentbus
# A mount left behind by a crash would make the next start fail.
ExecStopPost=-{fusermount} -uz %h/agentbus
Restart=on-failure

[Install]
WantedBy=default.target
""",
    }


def systemctl(*args, check=True):
    subprocess.run(["systemctl", "--user", *args], check=check)


def install(unit=UNIT, unit_files=files):
    for path, content in unit_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"wrote {path}")
    systemctl("daemon-reload")
    systemctl("enable", unit)
    systemctl("reset-failed", unit, check=False)  # clear a start-limit hit by earlier crashes
    systemctl("restart", unit)  # one already running picks up the new code
    print(f"{unit} enabled and (re)started")


def uninstall(unit=UNIT, unit_files=files):
    systemctl("disable", "--now", unit, check=False)
    for path in unit_files():
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    systemctl("daemon-reload")


if __name__ == "__main__":
    {"install": install, "uninstall": uninstall,
     "install-mount": lambda: install(MOUNT_UNIT, mount_files),
     "uninstall-mount": lambda: uninstall(MOUNT_UNIT, mount_files)}[sys.argv[1]]()
