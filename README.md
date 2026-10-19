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

## On freeq: #bots

A public proc also joins `#bots` on freeq as its own bot (`agentbus/freeq.py`), after it has its
rookery identity; `Procs(name="weather", freeq=True)` does only this part. It signs in with a
did:key kept where freeq's bot-kit keeps one (`~/.freeq/bots/<name>/agent.key`), runs bot-kit's
announce sequence (PROVENANCE naming its rookery DID as owner, AGENT REGISTER, AGENT MANIFEST,
PRESENCE, HEARTBEAT), joins `#bots`, and announces what it provides:

```
@+agentbus/provides=files.list,weather.current;+agentbus/host=<id> TAGMSG #bots
```

The same names go in its manifest under `[capabilities.channels] "#bots"`, so freeq's
`/api/v1/channels/%23bots/agent-capabilities` lists them too. It announces again when its
functions change and whenever someone sends `@+agentbus/discover=1 TAGMSG #bots`.

`examples/bridge.py` (`pixi run bridge`, one per host) joins `#bots`, asks who provides what, and
provides each announced function on the local hub. A local `procs.call("weather.current", ...)`
then goes as a tag-only DM to a proc in `#bots` that announced it
(`+agentbus/call`, `+agentbus/fn`, `+agentbus/args`), which answers with `+agentbus/result` or
`+agentbus/error`. Each remote proc gets its own connection to the hub, so the hub sees one
provider per proc: it ranks and fails over between them like local providers, and an app can fan
out with `providers(name)` and `call(name, provider=...)` (File → Open does this for
`files.list`). `bridge.owners()` maps those unique names to each proc's nick and DID.

- Only announcements from signed-in DIDs count; `AGENTBUS_TRUST=did:...,did:...` narrows that.
- A bridge never imports procs from its own bus (`+agentbus/host`).
- `AGENTBUS_FREEQ_SERVER` picks the server: `ircs://irc.freeq.at:6697` (default), or
  `wss://irc.freeq.at/irc`, freeq's WebSocket gateway, which also goes through `$HTTPS_PROXY`
  for sandboxes that only allow HTTPS out.
  `AGENTBUS_FREEQ_DEBUG=1` prints every line received.
- Limits come from freeq: 8 KiB lines (so args and results stay under ~7 KB each), and about
  10 commands per second per connection, which the bot paces itself to. A proc that crashes
  stays in `#bots` for 30 s (freeq's reconnect grace), so calls to it time out and fail over until
  then; a clean exit PARTs at once.

`tests/test_freeq.py` runs a bridge and two procs on separate buses against a freeq server named
by `AGENTBUS_FREEQ_TEST_SERVER` (e.g. a local `freeq-server` at `irc://127.0.0.1:6889`).

## Use it

```sh
pixi run test                # Python tests on a private bus (dbus-run-session)
pixi run demo                # hub + weather proc + an app calling and following weather
pixi run -e gui files-demo   # hub + a directory proc + a bot proc + the File -> Open GUI
```

The default and `gui` environments install the prebuilt `agentbus` conda package
(Rust core lib + PyO3 extension) from https://prefix.dev/channels/nandi-testing, so
nothing compiles locally. Building the crate lives in the `build` environment:

```sh
pixi run -e build test-rust      # Rust tests
pixi run -e build test-py-local  # build the extension locally and run the Python tests on it
pixi run -e build publish        # rattler-build linux-64 + linux-aarch64, upload to prefix.dev
```

After a core-lib change lands on main, run `publish` from a cloud container and then
`pixi update agentbus`. The build number is the main commit count, so no version bump is needed.

## Not yet

From PROCS.md: topics over freeq, signing in to freeq as the rookery DID instead of a did:key,
remote providers ranked after local ones (today the hub ranks bridged ones by latency like any
other), and typed front ends generated from lexicons.
