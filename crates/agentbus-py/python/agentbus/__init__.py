"""Networked procs on D-Bus.

A proc is an ordinary process that hosts functions and publishes topics::

    procs = agentbus.Procs()

    @procs.function("weather.current")
    def current(place):
        return {"temp_c": 11.5, "condition": "cloudy"}

    @procs.publisher("weather.*")
    def feed(place, stop):
        while not stop.wait(60):
            procs.publish("weather.current", place, current(place))

    procs.run_forever()

Apps call ``procs.call("weather.current", place="Berlin")`` and
``procs.subscribe("weather.*", "Berlin")`` without knowing which proc answers. ``Signals`` covers
plain broadcast request/response signals that need no hub.
"""

import threading
from collections import defaultdict

from ._native import (
    HUB_NAME,
    BusError,
    CallError,
    CallInfo,
    DemandWatch,
    Hub,
    Listener,
    NoProvider,
    Signals,
    Subscription,
    Topics,
    pattern_matches,
)
from ._native import Procs as _NativeProcs

__all__ = [
    "HUB_NAME", "BusError", "CallError", "CallInfo", "DemandWatch", "Hub", "Listener",
    "NoProvider", "Procs", "Signals", "Subscription", "Topics", "on_signal", "pattern_matches",
    "patterns_overlap",
]


def patterns_overlap(a, b):
    """Could a topic match both patterns ("weather.current", "weather.*", "*")?"""

    def prefix(p):
        return "" if p == "*" else p[:-1] if p.endswith(".*") else None

    pa, pb = prefix(a), prefix(b)
    if pa is not None and b.startswith(pa):
        return True
    if pb is not None and a.startswith(pb):
        return True
    return a == b


class Procs:
    """A proc's connection: host functions, call functions, publish and subscribe."""

    def __init__(self, hub=HUB_NAME):
        self._native = _NativeProcs(hub)
        self.topics = self._native.topics
        self._stopping = threading.Event()

    @property
    def unique_name(self):
        return self._native.unique_name

    def function(self, name, schema=""):
        """Decorator: provide ``name``; the function is called with the args as keyword
        arguments and returns a dict. ``schema`` is "" or a JSON string
        {"args": <JSON Schema>, "result": <JSON Schema>} the hub enforces."""

        def register(fn):
            self._native.provide(name, lambda args, info: fn(**args), schema)
            return fn

        return register

    def provide(self, name, handler, schema=""):
        """Provide ``name`` with ``handler(args, info) -> dict``."""
        self._native.provide(name, handler, schema)

    def withdraw(self, name):
        self._native.withdraw(name)

    def call(self, name, timeout=None, **args):
        """Call ``name`` through the hub. Raises NoProvider, TimeoutError or CallError."""
        return self._native.call(name, args, timeout)

    def functions(self):
        return self._native.functions()

    def providers(self, name):
        return self._native.providers(name)

    def publish(self, topic, subject, payload):
        return self.topics.publish(topic, subject, payload)

    def subscribe(self, pattern, subject=""):
        return self.topics.subscribe(pattern, subject)

    def watch_demand(self):
        return self.topics.watch_demand()

    def publisher(self, pattern):
        """Decorator: run ``fn(subject, stop)`` on its own thread while anyone watches a topic
        matching ``pattern`` for ``subject``; ``stop`` (a threading.Event) is set when the last
        watcher leaves. Demand for every subject ("") is ignored: there is nothing to produce."""

        def register(fn):
            watch = self.watch_demand()
            threading.Thread(target=self._follow_demand, args=(pattern, fn, watch), daemon=True).start()
            return fn

        return register

    def _follow_demand(self, pattern, fn, watch):
        watchers = defaultdict(int)  # (demand pattern, subject) -> subscribers
        running = {}  # subject -> stop event

        def on_demand(d):
            if not d["subject"] or not patterns_overlap(pattern, d["pattern"]):
                return
            subject = d["subject"]
            watchers[(d["pattern"], subject)] = d["subscribers"]
            total = sum(n for (_, s), n in watchers.items() if s == subject)
            if total and subject not in running:
                running[subject] = threading.Event()
                threading.Thread(target=fn, args=(subject, running[subject]), daemon=True).start()
            elif not total and subject in running:
                running.pop(subject).set()

        for d in watch.current:
            on_demand(d)
        while not self._stopping.is_set():
            try:
                on_demand(watch.next_change(timeout=0.5))
            except TimeoutError:
                continue
        for stop in running.values():
            stop.set()

    def run_forever(self):
        """Serve until Ctrl-C."""
        try:
            self._native.run_forever()
        finally:
            self._stopping.set()


def on_signal(signals, path, iface, member, callback):
    """Call ``callback(signal_dict)`` on a background thread for each matching signal.
    Returns a function that stops listening."""
    listener = signals.listen(path, iface, member)
    stop = threading.Event()

    def run():
        while not stop.is_set():
            try:
                callback(listener.next_signal(timeout=0.2))
            except TimeoutError:
                continue
        listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def cancel():
        stop.set()
        if threading.current_thread() is not thread:
            thread.join()

    return cancel
