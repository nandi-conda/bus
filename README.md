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

The hub owns `org.latha.agentbus.Hub`:

- `org.latha.agentbus.Functions1` at `/org/latha/agentbus/Functions`: `Provide(s name, s schema)`,
  `Withdraw(s)`, `Call(s name, a{sv} args, a{sv} options) -> a{sv}`, `Providers(s) -> a(sa{sv})`,
  `Functions() -> as`, signal `ProvidersChanged(s, u)`. `schema` is `""` or
  `{"args": <JSON Schema>, "result": <JSON Schema>}`, enforced on every call. Routing: the
  provider pinned in `functions.conf` (`function = bus.name` lines, at
  `$AGENTBUS_FUNCTIONS_CONF` or `~/.config/agentbus/functions.conf`), then the others by failure
  rate and latency, failing over on errors and malformed results. `options.timeout` defaults to
  25 s. Registrations are leases on the provider's connection; if the caller leaves or the
  deadline passes, the provider gets `Cancel`.
- Providers serve `org.latha.agentbus.Provider1` at `/org/latha/agentbus/Provider`:
  `Invoke(s name, a{sv} args, a{sv} meta) -> a{sv}` and `Cancel(s call_id)`, accepted only from
  the hub.
- `org.latha.agentbus.Topics1` at `/org/latha/agentbus/Topics`: `Subscribe(s pattern, s subject) -> o`
  creates a `org.latha.agentbus.Subscription1` whose `Event(s topic, s subject, a{sv} payload, t seq)`
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
own thread, listing every answer as it arrives. Opening a file calls `files.read` (args: `file`, a
name from the list; result: `{name, mime, size, encoding, content}`, `encoding` `utf-8` or `base64`,
at most 1 MiB) on the provider that listed it, and the window shows text in the editor and images in
an image view.

## Public procs

`Procs(name="weather", public=True)` makes a proc its own bot account on
[rookery-pds](https://github.com/nandi-conda/rookery-pds) (PROCS.md §4.1). On `run_forever` it
enrolls `$AGENTBUS_HANDLE` (default: its name) on `$AGENTBUS_PDS` with a WelcomeMat RSA-4096 key
(plus `$AGENTBUS_INVITE` on invite-gated instances), or loads the key it saved under
`$XDG_STATE_HOME/agentbus/procs/<name>/`. It then writes one `org.latha.agentbus.proc` record listing the
functions it provides and the topics it publishes (`@publisher(pattern, topics=[...],
subjects="place")`), and every payload it publishes carries `source: <its DID>`.
`examples/weather.py` is public; `--local` keeps it on the local bus.

```sh
AGENTBUS_PDS=https://rook.host AGENTBUS_INVITE=... pixi run weather
```

To try it without a deployed rookery, run one locally from rookery-pds with `wrangler dev
--var ROOKERY_HOSTNAME:localhost:8787` and use `AGENTBUS_PDS=http://localhost:8787`. Setting
`AGENTBUS_TEST_PDS` to that URL also runs `tests/test_public.py`.

## Remote procs: relay discovery + HTTPS

There is one hub per host and nothing else in between: a proc on another machine is reached by
finding it through the atproto relay and calling it over HTTPS. A proc can serve its functions as atproto XRPC over HTTPS (`agentbus/xrpc.py`), e.g. on
Modal. Bus function `weather.current` is method `org.latha.agentbus.weather.current` of service
`org.latha.agentbus.weather` (the NSID minus its last segment); lexicons are in `lexicons/`. The proc
writes one provider record in its rookery repo, collection `org.latha.agentbus.weather`, rkey `self`:
`{endpoint, methods, createdAt}` (rookery validates it).

The hub (`examples/hub.py`, `pixi run hub`) follows the services in `AGENTBUS_SERVICES`
(default `weather,files`): it asks the relay (`AGENTBUS_RELAY`, default `https://bsky.network`) for
`com.atproto.sync.listReposByCollection?collection=org.latha.agentbus.weather` every minute,
reads each repo's record from its PDS, and gives each provider its own connection to the hub
(`agentbus/relay.py`, `agentbus/mirror.py`). `procs.call("weather.current", place="Berlin")`
then becomes `GET <endpoint>/xrpc/org.latha.agentbus.weather.current?place=Berlin`;
string-only args go as query params, anything else as a POSTed JSON object.

Auth is atproto inter-service auth. The hub has its own rookery identity, enrolled on first
start on `AGENTBUS_PDS` (default: the project's rookery on Modal), and gets a short-lived JWT for each call from its PDS (`com.atproto.server.getServiceAuth`, `aud`
= the proc's DID, `lxm` = the method). A proc with an identity (`examples/weather_service.py`
with `AGENTBUS_PDS` and `AGENTBUS_ENDPOINT`) answers only callers whose JWT checks out against
the `#atproto` key in their DID document (`AGENTBUS_TRUST` narrows to a DID list). Without an
identity a proc serves anyone and writes no record, so nothing discovers it.

`examples/files/service.py` (`pixi run -e files-service files-service`) is the file proc as such
a service: `org.latha.agentbus.files.list` and `.read` serve an S3 bucket (`AGENTBUS_FILES_S3`,
`AGENTBUS_FILES_BUCKET`, default `agentbus-files`, the project's MinIO on Modal), so File -> Open
shows those files next to local ones.

The relay only lists repos it crawls: the PDS has to be up, deployed with the service-record
validation, and have asked the relay to crawl it (`ROOKERY_RELAY_HOSTS` includes `bsky.network`).

## Use it

```sh
pixi run test                # Python tests on a private bus (dbus-run-session)
pixi run demo                # hub + weather proc + an app calling and following weather
pixi run app Paris           # print Paris's weather and exit (needs a running hub)
pixi run app Paris --watch   # then keep following it until Ctrl-C (--watch 5: five events)
pixi run -e gui files-demo   # hub + a directory proc + a bot proc + the File -> Open GUI
pixi run -e gui files-gui    # the File -> Open GUI on this machine's hub (install-service)
pixi run files-provider dir docs ~/Documents   # serve a directory to File -> Open
```

The default and `gui` environments install the prebuilt `agentbus` conda package
(Rust core lib + PyO3 extension) from https://prefix.dev/channels/nandi-testing, so
nothing compiles locally. Building the crate lives in the `build` environment:

```sh
pixi run -e build test-rust      # Rust tests
pixi run -e build test-py-local  # build the extension locally and run the Python tests on it
pixi run -e build publish        # rattler-build linux-64 + linux-aarch64, upload to prefix.dev
```

After a change to `crates/` (Rust or `agentbus-py/python`) lands on main, run `publish` from a cloud container and then
`pixi update agentbus`. The build number is the main commit count, so no version bump is needed.

## Not yet

Topics from remote procs,
remote providers ranked after local ones (today the hub ranks bridged ones by latency like any
other), and typed front ends generated from lexicons.

## Running the hub as a service

`pixi run install-service` installs `agentbus-hub.service` as a systemd user unit (ExecStart is
this checkout's pixi env python), enables and starts it, and drops a D-Bus activation file so
the first call to the hub starts it too. `pixi run uninstall-service` removes both.
