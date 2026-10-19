"""A weather proc: provides weather.current, and publishes weather.current and weather.alert
only for the places someone watches. The readings are made up; a real proc would call a
weather API.

It is a public proc: its own bot account on rookery ($AGENTBUS_PDS, handle $AGENTBUS_HANDLE or
"weather"), with a dev.agentbus.proc record listing what it provides and publishes, and its DID
on every reading, and it joins #bots on freeq as its own bot. `--local` keeps it on the local bus
with no identity; `--local --freeq` puts it in #bots with only its freeq did:key."""

import json
import sys

import agentbus
from weather_readings import reading

SCHEMA = json.dumps({
    "args": {"type": "object", "required": ["place"],
             "properties": {"place": {"type": "string", "minLength": 1}}},
    "result": {"type": "object", "required": ["temp_c", "condition"]},
})

procs = agentbus.Procs(name="weather", description="Current conditions and alerts per place (demo readings)",
                       public="--local" not in sys.argv[1:], freeq="--freeq" in sys.argv[1:])


@procs.function("weather.current", schema=SCHEMA)
def current(place, tick=0):
    return reading(place, tick)


@procs.publisher("weather.*", topics=["weather.current", "weather.alert"], subjects="place")
def feed(place, stop):
    print(f"weather: {place} is watched, starting feed", flush=True)
    tick = 0
    while not stop.is_set():
        procs.publish("weather.current", place, current(place, tick))
        if tick % 3 == 2:
            procs.publish("weather.alert", place, {"severity": "minor", "headline": f"Gusty wind in {place}"})
        tick += 1
        stop.wait(0.7)
    print(f"weather: nobody watches {place} any more, stopping", flush=True)


print("weather: providing weather.current, publishing weather.* on demand", flush=True)
procs.run_forever()
