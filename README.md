# bus

`libagentbus` puts **networked procs** on D-Bus. A proc is an ordinary process (a weather API
wrapper, a calendar sync, a build runner) that hosts **functions** such as `weather.current` and
publishes **topics** such as `weather.alert` for `Berlin`. Apps call a function or subscribe to a
topic by name through the hub, never naming the proc. The design is
`agent-bus/PROCS.md` in the project files.

Everything is Python except the core library the bindings wrap.

| Part | Where |
|---|---|
| Rust core: `hub`, `functions`, `topics`, `signals` | `crates/libagentbus` |
| PyO3 bindings (`agentbus._native`) and the `agentbus` package (`Procs`, decorators) | `crates/agentbus-py` |
| Example procs and apps: `hub.py`, `weather.py`, `app.py`, `ui.py`, `watch.py`, `procs_demo.py` | `examples/` |
| File → Open GUI fanning out to every `files.list` proc | `examples/files/` |

## Python

```python
import agentbus

procs = agentbus.Procs()

@procs.function("weather.current")
def current(place):
    return {"temp_c": 11.5, "condition": "cloudy"}

@procs.publisher("weather.*")      # runs on its own thread while anyone watches a place
def feed(place, stop):
    while not stop.wait(60):
        procs.publish("weather.current", place, current(place))

procs.run_forever()
```

An app: `procs.call("weather.current", place="Berlin")`, `procs.subscribe("weather.*", "Berlin")`.
Calls raise `agentbus.NoProvider`, `TimeoutError` or `agentbus.CallError`.

## D-Bus surface

The hub owns `dev.agentbus.Hub`:

- `dev.agentbus.Functions1` at `/dev/agentbus/Functions`: `Provide(s name, s schema)`,
  `Withdraw(s)`, `Call(s name, a{sv} args, a{sv} options) -> a{sv}`, `Providers(s) -> a(sa{sv})`,
  `Functions() -> as`, signal `ProvidersChanged(s, u)`. `schema` is `""` or
  `{"args": <JSON Schema>, "result": <JSON Schema>}`, enforced on every call. Routing: the
  provider pinned in `functions.conf` (`function = bus.name` lines, at
  `$AGENTBUS_FUNCTIONS_CONF` or `~/.config/agentbus/functions.conf`), then the others by failure
  rate and latency, failing over on errors and malformed results. `options.timeout` defaults to
  25 s. Registrations are leases on the provider's connection; if the caller leaves or the
  deadline passes, the provider gets `Cancel`.
- Providers serve `dev.agentbus.Provider1` at `/dev/agentbus/Provider`:
  `Invoke(s name, a{sv} args, a{sv} meta) -> a{sv}` and `Cancel(s call_id)`, accepted only from
  the hub.
- `dev.agentbus.Topics1` at `/dev/agentbus/Topics`: `Subscribe(s pattern, s subject) -> o`
  creates a `dev.agentbus.Subscription1` whose `Event(s topic, s subject, a{sv} payload, t seq)`
  signals go to the subscriber only, starting with retained values. `Publish(s, s, a{sv}) -> u`
  fans out and retains. `Demand() -> a(ssu)` and `DemandChanged(s, s, u)` let publishers produce
  only what someone watches. Subscriptions end on `Unsubscribe()` or when the subscriber leaves.

Broadcast signals need no hub: `agentbus.Signals().broadcast(path, iface, member, *args)` and
`.listen(path, iface, member)`, with arguments typed the way D-Bus apps expect (str `s`, int `x`,
float `d`, list of str `as`, dict `a{sv}`).

A `Call` option `provider` (a unique name from `Providers`) asks just that provider, with no
failover, so an app can fan out to every provider of a function. `examples/files/` does this for
File → Open: file procs provide `files.list` (args: optional `path` prefix; result:
`{provider, files: [{name, uri, size, mtime, mime}]}`) and the dialog calls each provider on its
own thread, listing every answer as it arrives.

## Public procs

`Procs(name="weather", public=True)` makes a proc its own bot account on
[rookery-pds](https://github.com/nandi-conda/rookery-pds) (PROCS.md §4.1). On `run_forever` it
enrolls `$AGENTBUS_HANDLE` (default: its name) on `$AGENTBUS_PDS` with a WelcomeMat RSA-4096 key
(plus `$AGENTBUS_INVITE` on invite-gated instances), or loads the key it saved under
`$XDG_STATE_HOME/agentbus/procs/<name>/`. It then writes one `dev.agentbus.proc` record listing the
functions it provides and the topics it publishes (`@publisher(pattern, topics=[...],
subjects="place")`), and every payload it publishes carries `source: <its DID>`.
`examples/weather.py` is public; `--local` keeps it on the local bus.

```sh
AGENTBUS_PDS=https://rook.host AGENTBUS_INVITE=... pixi run weather
```

To try it without a deployed rookery, run one locally from rookery-pds with `wrangler dev
--var ROOKERY_HOSTNAME:localhost:8787` and use `AGENTBUS_PDS=http://localhost:8787`. Setting
`AGENTBUS_TEST_PDS` to that URL also runs `tests/test_public.py`.

## Use it

```sh
pixi run test                # Rust + Python tests on a private bus (dbus-run-session)
pixi run demo                # hub + weather proc + an app calling and following weather
pixi run -e gui files-demo   # hub + a directory proc + a bot proc + the File -> Open GUI
pixi run develop             # build the Python package into the pixi env
```

## Not yet

From PROCS.md: the public proc's freeq bot connection, the bridge, remote providers ranked after local ones, and typed front ends generated from
lexicons.
