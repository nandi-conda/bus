"""An app: asks for the weather once, then follows it. app.py [PLACE] [EVENTS]"""

import sys

import agentbus

place = sys.argv[1] if len(sys.argv) > 1 else "Berlin"
count = int(sys.argv[2]) if len(sys.argv) > 2 else 4

procs = agentbus.Procs()
print(f"app: weather.current({place}) = {procs.call('weather.current', place=place)}", flush=True)
sub = procs.subscribe("weather.*", place)
for _, e in zip(range(count), sub):
    print(f"app: #{e['seq']} {e['topic']} [{e['subject']}] {e['payload']}", flush=True)
sub.unsubscribe()
