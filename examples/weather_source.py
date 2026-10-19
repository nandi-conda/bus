"""A weather service that publishes weather.current (and the odd weather.alert) only for the
places someone is watching: it starts a feed when demand for a place appears and stops it when
the last subscriber leaves. The readings are made up; a real source would call a weather API."""

import threading
from collections import defaultdict

import agentbus

TOPICS = ("weather.current", "weather.alert")
topics = agentbus.Topics()


def feed(place, stop):
    base = sum(place.encode()) % 20
    tick = 0
    while not stop.is_set():
        reading = {"temp_c": float(base + tick % 5), "condition": "cloudy", "source": "weather_source"}
        topics.publish("weather.current", place, reading)
        if tick % 3 == 2:
            topics.publish("weather.alert", place, {"severity": "minor", "headline": f"Gusty wind in {place}"})
        tick += 1
        stop.wait(0.7)


watch = topics.watch_demand()
watchers = defaultdict(int)  # (pattern, place) -> subscribers
feeds = {}  # place -> stop event
print("weather_source: waiting for subscribers", flush=True)


def on_demand(d):
    # Subjects are places; "" (any place) gives us nothing to poll.
    if not d["subject"] or not any(agentbus.pattern_matches(d["pattern"], t) for t in TOPICS):
        return
    place = d["subject"]
    watchers[(d["pattern"], place)] = d["subscribers"]
    total = sum(n for (_, p), n in watchers.items() if p == place)
    if total and place not in feeds:
        print(f"weather_source: {place} is watched, starting feed", flush=True)
        feeds[place] = threading.Event()
        threading.Thread(target=feed, args=(place, feeds[place]), daemon=True).start()
    elif not total and place in feeds:
        feeds.pop(place).set()
        print(f"weather_source: nobody watches {place} any more, stopping", flush=True)


for d in watch.current:
    on_demand(d)
for d in watch:
    on_demand(d)
