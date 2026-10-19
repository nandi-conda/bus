"""An app: asks for the weather once, then follows it. app.py [PLACE] [EVENTS]"""

import sys
import time

import agentbus

place = sys.argv[1] if len(sys.argv) > 1 else "Berlin"
count = int(sys.argv[2]) if len(sys.argv) > 2 else 4

procs = agentbus.Procs()
# A hub that just started is still finding remote providers through the relay; give it a moment.
deadline = time.monotonic() + 30
while not procs.providers("weather.current") and time.monotonic() < deadline:
    time.sleep(0.5)
print(f"app: weather.current({place}) = {procs.call('weather.current', place=place)}", flush=True)
sub = procs.subscribe("weather.*", place)
for _, e in zip(range(count), sub):
    print(f"app: #{e['seq']} {e['topic']} [{e['subject']}] {e['payload']}", flush=True)
sub.unsubscribe()
