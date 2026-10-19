"""File -> Open demo: the hub, a directory proc, a bot proc, and the GUI on one bus.

    python demo.py           local procs only
    python demo.py --bots    also a bridge to #bots on freeq, and a file proc in #bots running on
                             a second private bus, standing in for another host

Run inside dbus-run-session (pixi run -e gui files-demo does). AGENTBUS_FREEQ_SERVER picks the
freeq server (default irc.freeq.at)."""

import os
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
py = sys.executable


def start(*args, **kw):
    return subprocess.Popen([py, *args], cwd=here, **kw)


def other_host(nick):
    """Another host's procs, on this (second) bus: a hub and a file proc in #bots. Runs until
    stdin closes, since signals to dbus-run-session do not reach its child."""
    procs = [start("../hub.py")]
    time.sleep(0.5)
    procs.append(start("provider.py", "bots", nick))
    sys.stdin.read()
    stop(procs)


def other_host_process(nick):
    return subprocess.Popen(["dbus-run-session", "--", py, "demo.py", "--host", nick],
                            cwd=here, stdin=subprocess.PIPE)


def stop(procs):
    for p in reversed(procs):
        if p.stdin:
            p.stdin.close()
        else:
            p.terminate()
        p.wait()


def main():
    if sys.argv[1:2] == ["--host"]:
        return other_host(sys.argv[2])
    bots = "--bots" in sys.argv
    gui_args = [a for a in sys.argv[1:] if a != "--bots"]

    procs = [start("../hub.py")]
    time.sleep(0.5)
    procs += [start("provider.py", "dir", "local:crates", "../../crates"),
              start("provider.py", "bot", "bot:notes")]
    if bots:
        procs += [start("../bridge.py"), other_host_process("files-notes")]
    try:
        sys.exit(subprocess.run([py, "gui.py", *gui_args], cwd=here).returncode)
    finally:
        stop(procs)


if __name__ == "__main__":
    main()
