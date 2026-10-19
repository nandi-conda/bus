# bus

`libagentbus` puts **tasks** on D-Bus: a caller submits a piece of work to an agent or a
capability slot and follows it through progress, questions and a final deliverable. The design
lives in the project's `agent-bus/` docs (`DESIGN.md`, `TASKS.md`); this is milestone M1 plus the
slot router.

| Part | Where |
|---|---|
| Rust core (`agentbus` crate: `Agent`, `submit`, `TaskContext`, `router`) | `crates/libagentbus` |
| Python bindings (PyO3, built with maturin) | `crates/agentbus-py` |
| Rust examples: `greeter`, `router`, `agent`, `sender` | `crates/libagentbus/examples` |
| App-domain topics (`agentbus::topics`) and examples `topics_hub`, `weather_source`, `watch` | `crates/libagentbus/src/topics.rs`, `examples` |
| Python examples: `submit_task.py`, `agent.py` | `examples/` |

## D-Bus surface

- `org.agentbus.Agent1` at `/org/agentbus/Agent`: `Submit(a{sv} spec) -> o task`, properties
  `TaskClasses`, `Load`, `Status`. Tasks are children of an `ObjectManager` at the same path.
- `org.agentbus.Task1` at `/org/agentbus/Agent/task/<n>`: properties `State`, `Class`, `Goal`,
  `Owner`; methods `Cancel(s)`, `Respond(s id, a{sv} answer)`, `Result() -> (a{sv}, a{sv})`;
  signals `Progress(d, s)`, `Question(s id, s text, s schema)`, `Log(s, s)`, `Finished(s)`.
- `org.agentbus.Router1` on a slot such as `org.agentbus.cap.Plan1`: `Register()` adds the
  caller; each task submitted to the slot goes round-robin to a live member, and the router's own
  task mirrors the member's signals and relays answers and cancels.

- `dev.agentbus.Topics1` on `dev.agentbus.Topics` at `/dev/agentbus/Topics`: app-domain
  subscriptions, independent of agents and tasks. `Subscribe(s pattern, s subject) -> o`
  (pattern `weather.current`, `weather.*` or `*`; subject `Berlin`, or `""` for any) creates a
  `dev.agentbus.Subscription1` object whose `Event(s topic, s subject, a{sv} payload, t seq)`
  signals go to the subscriber only, starting with the retained current values. `Publish(s topic,
  s subject, a{sv} payload) -> u` fans out and retains. `Demand() -> a(ssu)` and the
  `DemandChanged(s, s, u)` signal let a publisher produce only what someone watches.
  Subscriptions end on `Unsubscribe()` or when the subscriber leaves the bus.

States follow A2A: `submitted`, `working`, `input-required`, `completed`, `failed`,
`rejected`, `canceled`.

## Use it

```sh
pixi run test           # Rust + Python tests on a private bus (dbus-run-session)
pixi run demo           # router + Rust agent + Rust sender
pixi run py-demo        # router + Rust agent, Python submits the task
pixi run py-agent-demo  # router + Python agent, Rust sender
pixi run topics-demo    # topics hub + weather source + an app watching Berlin
pixi run develop        # build the Python module into the pixi env
```

```python
import agentbus

task = agentbus.Client().submit("org.agentbus.cap.Plan1", "dev.agentbus.task.plan", "build a kv store")
for event in task:
    if event["type"] == "question":
        task.respond(event["id"], {"language": "python"})
print(task.result())
```

## File → Open example

`examples/files/` is an ordinary PySide6 app with no chat box: File → Open broadcasts
`org.example.Files1.GetFiles(s request)` on the session bus, and every provider that hears it
broadcasts `SendFiles(s request, s provider, as files)` back. The dialog lists whatever arrives,
tagged by provider, so an agent shows up in the app's own Open dialog next to the local disk.

```sh
pixi run -e gui files-demo   # a directory provider, an agent provider, and the GUI on a private bus
```

## Not yet

Artifacts (fd passing), `Amend`, `Quote`/award, budgets and grants, and the SQLite journal from
TASKS.md are not implemented.
