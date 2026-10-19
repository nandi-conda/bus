"""A weather proc: provides weather.current, and publishes weather.current and weather.alert
only for the places someone watches. The readings are made up; a real proc would call a
weather API."""

import json

import agentbus

SCHEMA = json.dumps({
    "args": {"type": "object", "required": ["place"],
             "properties": {"place": {"type": "string", "minLength": 1}}},
    "result": {"type": "object", "required": ["temp_c", "condition"]},
})

procs = agentbus.Procs()


@procs.function("weather.current", schema=SCHEMA)
def current(place, tick=0):
    return {"temp_c": float(sum(place.encode()) % 20 + tick % 5), "condition": "cloudy"}


@procs.publisher("weather.*")
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
