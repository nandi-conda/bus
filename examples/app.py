"""An app: asks for the weather once and exits. app.py [PLACE] [--watch [EVENTS]]
--watch then follows weather.* for that place (EVENTS events, or until Ctrl-C)."""

import argparse
import time

import agentbus

parser = argparse.ArgumentParser()
parser.add_argument("place", nargs="?", default="Berlin")
parser.add_argument("--watch", nargs="?", type=int, const=0, metavar="EVENTS",
                    help="follow weather.* after the call; EVENTS stops after that many (default: forever)")
args = parser.parse_args()

procs = agentbus.Procs()
# The provider may be a remote proc that scales to zero, hence the long call timeout.
# A hub that just started is still finding remote providers through the relay; give it a moment.
deadline = time.monotonic() + 30
while not procs.providers("weather.current") and time.monotonic() < deadline:
    time.sleep(0.5)
print(f"app: weather.current({args.place}) = {procs.call('weather.current', timeout=90, place=args.place)}", flush=True)
if args.watch is not None:
    sub = procs.subscribe("weather.*", args.place)
    try:
        for n, e in enumerate(sub, 1):
            print(f"app: #{e['seq']} {e['topic']} [{e['subject']}] {e['payload']}", flush=True)
            if n == args.watch:
                break
    except KeyboardInterrupt:
        pass
    sub.unsubscribe()
