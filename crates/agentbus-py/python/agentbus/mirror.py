"""Stand-ins for remote procs on the local hub.

Relay discovery (``agentbus.relay``) learns which remote procs provide which functions and hands
that to a ``Mirror``. The mirror gives each remote proc its own connection to the local hub,
providing the functions it offers and forwarding each call to ``forward(remote, name, args)``.
So the hub sees one provider per remote proc: it ranks them, fails over between them, and an
app can list them with ``providers(name)`` and call one with ``call(name, provider=...)``.

D-Bus calls happen on the mirror's own thread, never on the caller's, so a network reader
keeps flowing while the hub catches up."""

import threading


class Remote:
    """One remote proc: ``key`` identifies it to whoever found it, ``label`` names it to people (a
    nick, a handle or endpoint), ``did`` is who it is, ``functions`` what it provides."""

    def __init__(self, key, label, did, functions):
        self.key, self.label, self.did, self.functions = key, label, did, set(functions)
        self.procs = None  # agentbus.Procs on the local hub, made by the sync thread
        self.provided = set()


class Mirror:
    def __init__(self, hub, forward, log_prefix="mirror"):
        self.hub = hub
        self.forward = forward
        self.log_prefix = log_prefix
        self.lock = threading.Lock()
        self.remotes = {}  # key -> Remote; finders change it under ``lock``, then call changed()
        self._standing = {}  # Remote -> True, for every remote that still has a hub connection
        self._dirty = threading.Event()
        self._requested = 0  # changes asked for; _done of them have reached the hub
        self._done = 0
        self._stopping = threading.Event()
        self._synced = threading.Condition()
        self._thread = threading.Thread(target=self._sync_loop, name=f"{log_prefix}-sync", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        with self.lock:
            self.remotes.clear()
        self._stopping.set()
        self.changed()
        self._thread.join(10)

    def changed(self):
        with self._synced:
            self._requested += 1
        self._dirty.set()

    def put(self, key, label, did, functions):
        """Add or update one remote proc. A known key with a new DID is a different proc."""
        with self.lock:
            remote = self.remotes.get(key)
            if remote and remote.did == did:
                remote.label, remote.functions = label, set(functions)
            else:
                self.remotes[key] = Remote(key, label, did, functions)
        self.changed()

    def drop(self, key):
        with self.lock:
            self.remotes.pop(key, None)
        self.changed()

    def functions(self):
        """The functions this mirror currently provides on the local hub."""
        with self.lock:
            return set().union(*(r.provided for r in self._standing))

    def providers(self, name):
        """[(label, did)] of the remote procs providing ``name`` on the local hub."""
        with self.lock:
            return sorted((r.label, r.did) for r in self._standing if name in r.provided)

    def owners(self):
        """{local unique bus name: (label, did)}, to label what ``Procs.providers`` returns."""
        with self.lock:
            return {r.procs.unique_name: (r.label, r.did) for r in self._standing if r.procs}

    def wait_synced(self, timeout=10):
        """Block until pending changes have reached the hub (for tests and scripts)."""
        with self._synced:
            target = self._requested
            return self._synced.wait_for(lambda: self._done >= target, timeout)

    def _sync_loop(self):
        while True:
            self._dirty.wait()
            self._dirty.clear()
            with self._synced:
                target = self._requested
            self._sync()
            with self._synced:
                self._done = target
                self._synced.notify_all()
            if self._stopping.is_set() and not self._dirty.is_set():
                return

    def _sync(self):
        """Give every remote proc a hub connection providing what it offers; drop the rest."""
        from . import Procs

        with self.lock:
            live = list(self.remotes.values())
            gone = [r for r in self._standing if r not in live]
        for remote in gone:
            for name in sorted(remote.provided):
                try:
                    remote.procs.withdraw(name)
                except Exception:
                    pass
            with self.lock:
                remote.provided = set()
                self._standing.pop(remote, None)
            remote.procs = None  # dropping the connection releases anything left
        for remote in live:
            try:
                if remote.procs is None:
                    remote.procs = Procs(self.hub)
                for name in sorted(remote.functions - remote.provided):
                    remote.procs.provide(name, self._forwarder(remote, name))
                    with self.lock:
                        remote.provided.add(name)
                        self._standing[remote] = True
                for name in sorted(remote.provided - remote.functions):
                    remote.procs.withdraw(name)
                    with self.lock:
                        remote.provided.discard(name)
            except Exception as e:
                print(f"{self.log_prefix}: could not provide {remote.label}'s functions: {e}", flush=True)

    def _forwarder(self, remote, name):
        def forward(args, info):
            if info is not None and info.cancelled:
                raise TimeoutError("call canceled")
            return self.forward(remote, name, args)

        return forward
