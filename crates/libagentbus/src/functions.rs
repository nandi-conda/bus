//! Functions: procs host named calls such as `weather.current`, and apps call them by name
//! through the hub, never naming the proc.
//!
//! The hub (see [`crate::hub`]) serves `dev.agentbus.Functions1` at [`FUNCTIONS_PATH`]:
//! - `Provide(s name, s schema)` makes the caller a provider of `name` for as long as it stays on
//!   the bus. `schema` is `""` or a JSON object `{"args": <JSON Schema>, "result": <JSON Schema>}`
//!   (both optional) that the hub enforces on every call.
//! - `Withdraw(s name)` stops providing.
//! - `Call(s name, a{sv} args, a{sv} options) -> a{sv}` routes to a provider: the pinned one if
//!   `functions.conf` names one and it is on the bus, then the others by failure rate and recent
//!   latency. A provider that errors, times out or returns a malformed result is skipped for the
//!   next one. `options.timeout` (seconds, default 25) bounds the whole call.
//! - `Providers(s name) -> a(sa{sv})`, `Functions() -> as`, signal `ProvidersChanged(s, u)`.
//!
//! Providers serve `dev.agentbus.Provider1` at [`PROVIDER_PATH`]:
//! `Invoke(s name, a{sv} args, a{sv} meta) -> a{sv}` and `Cancel(s call_id)`, accepted only from
//! the hub. `meta` carries `call_id`, `caller` (the app's unique name) and `deadline_ms`. When an
//! app leaves the bus mid-call, or a call times out, the hub sends `Cancel`.

use std::{
    collections::{BTreeMap, HashMap},
    future::Future,
    path::Path,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

use serde_json::{Value as Json, json};
use tokio::sync::watch;
use zbus::{Connection, connection, fdo::DBusProxy, interface, message::Header, names::BusName};

use crate::{Dict, Error, Result, dict_to_json, json_to_dict, topics};

pub const FUNCTIONS_PATH: &str = "/dev/agentbus/Functions";
pub const FUNCTIONS_IFACE: &str = "dev.agentbus.Functions1";
pub const PROVIDER_PATH: &str = "/dev/agentbus/Provider";
pub const PROVIDER_IFACE: &str = "dev.agentbus.Provider1";
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(25);

/// Pinned providers: function name -> bus name of the proc that must answer it.
pub type Pins = HashMap<String, String>;

/// Read `functions.conf`: one `function = bus.name` per line, `#` comments. A missing file is
/// no pins.
pub fn load_pins(path: &Path) -> Result<Pins> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Pins::new()),
        Err(e) => return Err(Error::Value(format!("cannot read {}: {e}", path.display()))),
    };
    let mut pins = Pins::new();
    for (n, line) in text.lines().enumerate() {
        let line = line.split('#').next().unwrap_or("").trim();
        if line.is_empty() {
            continue;
        }
        let bad = || {
            Error::Value(format!(
                "{}:{}: expected `function = bus.name`",
                path.display(),
                n + 1
            ))
        };
        let (name, proc) = line.split_once('=').ok_or_else(bad)?;
        let (name, proc) = (name.trim(), proc.trim());
        topics::check_topic(name).map_err(|_| bad())?;
        BusName::try_from(proc).map_err(|_| bad())?;
        pins.insert(name.to_string(), proc.to_string());
    }
    Ok(pins)
}

/// The default `functions.conf`: `$AGENTBUS_FUNCTIONS_CONF`, else
/// `$XDG_CONFIG_HOME/agentbus/functions.conf`, else `~/.config/agentbus/functions.conf`.
pub fn default_pins_path() -> Option<std::path::PathBuf> {
    if let Some(p) = std::env::var_os("AGENTBUS_FUNCTIONS_CONF") {
        return Some(p.into());
    }
    let config = std::env::var_os("XDG_CONFIG_HOME")
        .map(std::path::PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| Path::new(&h).join(".config")))?;
    Some(config.join("agentbus").join("functions.conf"))
}

struct Schema {
    raw: String,
    args: Option<jsonschema::Validator>,
    result: Option<jsonschema::Validator>,
}

impl Schema {
    fn parse(raw: &str) -> zbus::fdo::Result<Schema> {
        let invalid = |m: String| zbus::fdo::Error::InvalidArgs(format!("bad schema: {m}"));
        if raw.trim().is_empty() {
            return Ok(Schema {
                raw: String::new(),
                args: None,
                result: None,
            });
        }
        let json: Json = serde_json::from_str(raw).map_err(|e| invalid(e.to_string()))?;
        let map = json
            .as_object()
            .ok_or_else(|| invalid("not a JSON object".into()))?;
        if let Some(k) = map
            .keys()
            .find(|k| !["args", "result"].contains(&k.as_str()))
        {
            return Err(invalid(format!(
                "unknown key {k:?}, expected args and result"
            )));
        }
        let compile = |k: &str| {
            map.get(k)
                .map(|s| jsonschema::validator_for(s).map_err(|e| invalid(format!("{k}: {e}"))))
                .transpose()
        };
        Ok(Schema {
            raw: raw.to_string(),
            args: compile("args")?,
            result: compile("result")?,
        })
    }
}

fn check(
    validator: &Option<jsonschema::Validator>,
    value: &Json,
) -> std::result::Result<(), String> {
    match validator {
        Some(v) => v.validate(value).map_err(|e| e.to_string()),
        None => Ok(()),
    }
}

struct Entry {
    owner: String,
    schema: Schema,
    calls: u64,
    failures: u64,
    /// Moving average of successful call latency, in milliseconds.
    latency_ms: f64,
}

impl Entry {
    fn failure_rate(&self) -> f64 {
        if self.calls == 0 {
            0.0
        } else {
            self.failures as f64 / self.calls as f64
        }
    }
}

struct InFlight {
    caller: String,
    provider: String,
    canceled: Arc<AtomicBool>,
}

#[derive(Default)]
pub(crate) struct HubState {
    functions: BTreeMap<String, Vec<Entry>>,
    inflight: HashMap<String, InFlight>,
    next_call: u64,
    pins: Pins,
}

pub(crate) type State = Arc<Mutex<HubState>>;

fn lock(state: &State) -> std::sync::MutexGuard<'_, HubState> {
    state.lock().expect("functions state lock poisoned")
}

pub(crate) struct HubObject {
    state: State,
}

fn sender(header: &Header<'_>) -> zbus::fdo::Result<String> {
    header
        .sender()
        .map(|s| s.to_string())
        .ok_or_else(|| zbus::fdo::Error::Failed("call has no sender".into()))
}

enum Attempt {
    Done(Json),
    Next(String),
    TimedOut,
}

#[interface(name = "dev.agentbus.Functions1")]
impl HubObject {
    async fn provide(
        &self,
        name: String,
        schema: String,
        #[zbus(header)] header: Header<'_>,
        #[zbus(connection)] conn: &Connection,
    ) -> zbus::fdo::Result<()> {
        topics::check_topic(&name)?;
        let schema = Schema::parse(&schema)?;
        let owner = sender(&header)?;
        let count = {
            let mut st = lock(&self.state);
            let entries = st.functions.entry(name.clone()).or_default();
            entries.retain(|e| e.owner != owner);
            entries.push(Entry {
                owner,
                schema,
                calls: 0,
                failures: 0,
                latency_ms: 0.0,
            });
            entries.len() as u32
        };
        emit_providers_changed(conn, &name, count).await?;
        Ok(())
    }

    async fn withdraw(
        &self,
        name: String,
        #[zbus(header)] header: Header<'_>,
        #[zbus(connection)] conn: &Connection,
    ) -> zbus::fdo::Result<()> {
        let owner = sender(&header)?;
        let count = {
            let mut st = lock(&self.state);
            let entries = st.functions.get_mut(&name);
            let Some(entries) = entries.filter(|e| e.iter().any(|e| e.owner == owner)) else {
                return Err(zbus::fdo::Error::InvalidArgs(format!(
                    "you do not provide {name}"
                )));
            };
            entries.retain(|e| e.owner != owner);
            let n = entries.len() as u32;
            if n == 0 {
                st.functions.remove(&name);
            }
            n
        };
        emit_providers_changed(conn, &name, count).await?;
        Ok(())
    }

    async fn call(
        &self,
        name: String,
        args: Dict,
        options: Dict,
        #[zbus(header)] header: Header<'_>,
        #[zbus(connection)] conn: &Connection,
    ) -> zbus::fdo::Result<Dict> {
        let caller = sender(&header)?;
        let args = dict_to_json(&args)?;
        let (timeout, only) = call_options(&dict_to_json(&options)?)?;
        let deadline = Instant::now() + timeout;
        let canceled = Arc::new(AtomicBool::new(false));

        let mut last_error = None;
        let candidates = match only {
            // A caller fanning out to every provider names one; no failover to the others.
            Some(p) if lock(&self.state).entry(&name, &p).is_some() => vec![p],
            Some(p) => {
                return Err(zbus::fdo::Error::ServiceUnknown(format!(
                    "{p} does not provide {name}"
                )));
            }
            None => self.candidates(conn, &name).await?,
        };
        for provider in candidates {
            if canceled.load(Ordering::Relaxed) {
                return Err(zbus::fdo::Error::Failed("the caller left the bus".into()));
            }
            let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                break;
            };
            // Bad args are the caller's mistake, not the provider's: no failover.
            {
                let st = lock(&self.state);
                if let Some(e) = st.entry(&name, &provider) {
                    check(&e.schema.args, &args)
                        .map_err(|m| zbus::fdo::Error::InvalidArgs(format!("{name} args: {m}")))?;
                }
            }
            match self
                .attempt(conn, &name, &provider, &caller, &args, remaining, &canceled)
                .await
            {
                Attempt::Done(result) => return Ok(json_to_dict(&result)?),
                Attempt::Next(e) => last_error = Some(e),
                Attempt::TimedOut => break,
            }
        }
        match last_error {
            _ if Instant::now() >= deadline => Err(zbus::fdo::Error::Timeout(format!(
                "{name} did not answer within {}s",
                timeout.as_secs_f64()
            ))),
            Some(e) => Err(zbus::fdo::Error::Failed(format!(
                "{name}: every provider failed; last: {e}"
            ))),
            None => Err(zbus::fdo::Error::ServiceUnknown(format!(
                "nobody provides {name}"
            ))),
        }
    }

    fn providers(&self, name: String) -> Vec<(String, Dict)> {
        let st = lock(&self.state);
        let pinned = st.pins.get(&name);
        st.functions
            .get(&name)
            .into_iter()
            .flatten()
            .filter_map(|e| {
                let stats = json!({
                    "calls": e.calls,
                    "failures": e.failures,
                    "latency_ms": e.latency_ms,
                    "pinned": pinned == Some(&e.owner),
                    "schema": e.schema.raw,
                });
                Some((e.owner.clone(), json_to_dict(&stats).ok()?))
            })
            .collect()
    }

    fn functions(&self) -> Vec<String> {
        lock(&self.state).functions.keys().cloned().collect()
    }

    #[zbus(signal)]
    async fn providers_changed(
        emitter: &zbus::object_server::SignalEmitter<'_>,
        name: &str,
        count: u32,
    ) -> zbus::Result<()>;
}

impl HubState {
    fn entry(&self, name: &str, owner: &str) -> Option<&Entry> {
        self.functions.get(name)?.iter().find(|e| e.owner == owner)
    }

    fn entry_mut(&mut self, name: &str, owner: &str) -> Option<&mut Entry> {
        self.functions
            .get_mut(name)?
            .iter_mut()
            .find(|e| e.owner == owner)
    }
}

impl HubObject {
    /// Providers of `name` in the order to try them: the pinned one, then best record first.
    async fn candidates(&self, conn: &Connection, name: &str) -> zbus::fdo::Result<Vec<String>> {
        let pin = lock(&self.state).pins.get(name).cloned();
        let pinned = match pin {
            Some(p) => DBusProxy::new(conn)
                .await?
                .get_name_owner(BusName::try_from(p.as_str())?)
                .await
                .ok()
                .map(|o| o.to_string()),
            None => None,
        };
        let st = lock(&self.state);
        let mut entries: Vec<&Entry> = st.functions.get(name).into_iter().flatten().collect();
        entries.sort_by(|a, b| {
            let first = |e: &Entry| Some(&e.owner) != pinned.as_ref();
            (first(a), a.failure_rate(), a.latency_ms)
                .partial_cmp(&(first(b), b.failure_rate(), b.latency_ms))
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        Ok(entries.into_iter().map(|e| e.owner.clone()).collect())
    }

    #[allow(clippy::too_many_arguments)]
    async fn attempt(
        &self,
        conn: &Connection,
        name: &str,
        provider: &str,
        caller: &str,
        args: &Json,
        remaining: Duration,
        canceled: &Arc<AtomicBool>,
    ) -> Attempt {
        let call_id = {
            let mut st = lock(&self.state);
            st.next_call += 1;
            let id = format!("c{}", st.next_call);
            st.inflight.insert(
                id.clone(),
                InFlight {
                    caller: caller.into(),
                    provider: provider.into(),
                    canceled: canceled.clone(),
                },
            );
            id
        };
        let meta = json!({"call_id": call_id, "caller": caller, "deadline_ms": remaining.as_millis() as u64});
        let started = Instant::now();
        let reply =
            tokio::time::timeout(remaining, invoke(conn, provider, name, args, &meta)).await;
        lock(&self.state).inflight.remove(&call_id);

        let outcome = match reply {
            Err(_) => {
                send_cancel(conn, provider, &call_id);
                Attempt::TimedOut
            }
            Ok(Err(e)) => Attempt::Next(e.to_string()),
            Ok(Ok(result)) => {
                let st = lock(&self.state);
                match st
                    .entry(name, provider)
                    .map(|e| check(&e.schema.result, &result))
                {
                    Some(Err(m)) => {
                        Attempt::Next(format!("{provider} returned a malformed result: {m}"))
                    }
                    _ => Attempt::Done(result),
                }
            }
        };
        let mut st = lock(&self.state);
        if let Some(e) = st.entry_mut(name, provider) {
            e.calls += 1;
            match &outcome {
                Attempt::Done(_) => {
                    let ms = started.elapsed().as_secs_f64() * 1000.0;
                    e.latency_ms = if e.calls == 1 {
                        ms
                    } else {
                        0.8 * e.latency_ms + 0.2 * ms
                    };
                }
                _ => e.failures += 1,
            }
        }
        outcome
    }
}

/// The `timeout` and `provider` call options.
fn call_options(options: &Json) -> zbus::fdo::Result<(Duration, Option<String>)> {
    let map = options.as_object().expect("dict_to_json gives an object");
    if let Some(k) = map
        .keys()
        .find(|k| !["timeout", "provider"].contains(&k.as_str()))
    {
        return Err(zbus::fdo::Error::InvalidArgs(format!(
            "unknown call option {k:?}"
        )));
    }
    let timeout = match map.get("timeout") {
        None => DEFAULT_TIMEOUT,
        Some(t) => t
            .as_f64()
            .filter(|t| *t > 0.0 && t.is_finite())
            .map(Duration::from_secs_f64)
            .ok_or_else(|| {
                zbus::fdo::Error::InvalidArgs("timeout must be a positive number of seconds".into())
            })?,
    };
    let provider = match map.get("provider") {
        None => None,
        Some(Json::String(p)) => Some(p.clone()),
        Some(_) => {
            return Err(zbus::fdo::Error::InvalidArgs(
                "provider must be a unique bus name".into(),
            ));
        }
    };
    Ok((timeout, provider))
}

async fn invoke(
    conn: &Connection,
    provider: &str,
    name: &str,
    args: &Json,
    meta: &Json,
) -> Result<Json> {
    let reply = conn
        .call_method(
            Some(provider),
            PROVIDER_PATH,
            Some(PROVIDER_IFACE),
            "Invoke",
            &(name, json_to_dict(args)?, json_to_dict(meta)?),
        )
        .await?;
    let result: Dict = reply.body().deserialize()?;
    dict_to_json(&result)
}

fn send_cancel(conn: &Connection, provider: &str, call_id: &str) {
    let (conn, provider, call_id) = (conn.clone(), provider.to_string(), call_id.to_string());
    tokio::spawn(async move {
        let _ = conn
            .call_method(
                Some(provider.as_str()),
                PROVIDER_PATH,
                Some(PROVIDER_IFACE),
                "Cancel",
                &(call_id,),
            )
            .await;
    });
}

async fn emit_providers_changed(conn: &Connection, name: &str, count: u32) -> zbus::Result<()> {
    conn.emit_signal(
        None::<&str>,
        FUNCTIONS_PATH,
        FUNCTIONS_IFACE,
        "ProvidersChanged",
        &(name, count),
    )
    .await
}

/// The functions half of the hub, ready to serve at [`FUNCTIONS_PATH`].
pub(crate) fn hub_object(pins: Pins) -> (HubObject, State) {
    let state: State = Arc::new(Mutex::new(HubState {
        pins,
        ..Default::default()
    }));
    (
        HubObject {
            state: state.clone(),
        },
        state,
    )
}

/// Registrations are leases, and so are calls: when `name` leaves the bus, it stops providing,
/// and providers working on its calls are told to cancel them.
pub(crate) async fn drop_owner(conn: &Connection, state: &State, name: &str) -> zbus::Result<()> {
    let (changed, cancels) = {
        let mut st = lock(state);
        let mut changed = Vec::new();
        st.functions.retain(|fname, entries| {
            let before = entries.len();
            entries.retain(|e| e.owner != name);
            if entries.len() != before {
                changed.push((fname.clone(), entries.len() as u32));
            }
            !entries.is_empty()
        });
        let cancels: Vec<(String, String)> = st
            .inflight
            .iter()
            .filter(|(_, f)| f.caller == name)
            .map(|(id, f)| {
                f.canceled.store(true, Ordering::Relaxed);
                (f.provider.clone(), id.clone())
            })
            .collect();
        (changed, cancels)
    };
    for (provider, id) in cancels {
        send_cancel(conn, &provider, &id);
    }
    for (fname, n) in changed {
        emit_providers_changed(conn, &fname, n).await?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------------------------
// The proc side.

/// What a function handler learns about the call it serves.
#[derive(Clone)]
pub struct CallInfo {
    pub call_id: String,
    /// The calling app's unique bus name.
    pub caller: String,
    cancel: watch::Receiver<bool>,
}

impl CallInfo {
    pub fn is_cancelled(&self) -> bool {
        *self.cancel.borrow()
    }

    /// Resolves once the hub cancels this call.
    pub async fn cancelled(&self) {
        let _ = self.cancel.clone().wait_for(|c| *c).await;
    }
}

/// Serves one function: args in, result (a JSON object) or an error message out.
pub type FunctionHandler = Arc<
    dyn Fn(
            Json,
            CallInfo,
        ) -> Pin<Box<dyn Future<Output = std::result::Result<Json, String>> + Send>>
        + Send
        + Sync,
>;

/// Wrap an async closure as a [`FunctionHandler`].
pub fn function<F, Fut>(f: F) -> FunctionHandler
where
    F: Fn(Json, CallInfo) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = std::result::Result<Json, String>> + Send + 'static,
{
    Arc::new(move |args, info| Box::pin(f(args, info)))
}

type Handlers = Arc<Mutex<HashMap<String, FunctionHandler>>>;

struct ProviderObject {
    hub: String,
    handlers: Handlers,
    calls: Arc<Mutex<HashMap<String, watch::Sender<bool>>>>,
}

impl ProviderObject {
    /// Only the hub may invoke or cancel: anyone else could bypass routing and validation.
    async fn check_hub(&self, conn: &Connection, header: &Header<'_>) -> zbus::fdo::Result<()> {
        let hub = topics::owner_of(conn, &self.hub).await.ok();
        if hub.is_none() || hub.as_deref() != header.sender().map(|s| s.as_str()) {
            return Err(zbus::fdo::Error::AccessDenied(format!(
                "only {} may call providers",
                self.hub
            )));
        }
        Ok(())
    }
}

#[interface(name = "dev.agentbus.Provider1")]
impl ProviderObject {
    async fn invoke(
        &self,
        name: String,
        args: Dict,
        meta: Dict,
        #[zbus(header)] header: Header<'_>,
        #[zbus(connection)] conn: &Connection,
    ) -> zbus::fdo::Result<Dict> {
        self.check_hub(conn, &header).await?;
        let handler = self
            .handlers
            .lock()
            .expect("handlers lock poisoned")
            .get(&name)
            .cloned();
        let handler = handler.ok_or_else(|| {
            zbus::fdo::Error::UnknownMethod(format!("{name} is not provided here"))
        })?;
        let meta = dict_to_json(&meta)?;
        let field = |k: &str| meta[k].as_str().unwrap_or_default().to_string();
        let (tx, rx) = watch::channel(false);
        let call_id = field("call_id");
        self.calls
            .lock()
            .expect("calls lock poisoned")
            .insert(call_id.clone(), tx);
        let info = CallInfo {
            call_id: call_id.clone(),
            caller: field("caller"),
            cancel: rx,
        };
        // Biased: a handler that watches `cancelled()` itself gets to finish first.
        let result = tokio::select! {
            biased;
            r = handler(dict_to_json(&args)?, info.clone()) => r,
            _ = info.cancelled() => Err("canceled".into()),
        };
        self.calls
            .lock()
            .expect("calls lock poisoned")
            .remove(&call_id);
        let result = result.map_err(zbus::fdo::Error::Failed)?;
        json_to_dict(&result)
            .map_err(|e| zbus::fdo::Error::Failed(format!("{name} returned a bad result: {e}")))
    }

    async fn cancel(
        &self,
        call_id: String,
        #[zbus(header)] header: Header<'_>,
        #[zbus(connection)] conn: &Connection,
    ) -> zbus::fdo::Result<()> {
        self.check_hub(conn, &header).await?;
        if let Some(tx) = self
            .calls
            .lock()
            .expect("calls lock poisoned")
            .get(&call_id)
        {
            tx.send_replace(true);
        }
        Ok(())
    }
}

/// A proc's connection to the bus: hosts functions, and is the identity it publishes and calls
/// with.
pub struct Procs {
    conn: Connection,
    hub: String,
    handlers: Handlers,
}

impl Procs {
    /// Connect to the session bus, serving `Provider1` for the hub at `hub`.
    pub async fn connect(hub: &str) -> Result<Procs> {
        let handlers: Handlers = Default::default();
        let conn = connection::Builder::session()
            .serve_at(
                PROVIDER_PATH,
                ProviderObject {
                    hub: hub.to_string(),
                    handlers: handlers.clone(),
                    calls: Default::default(),
                },
            )
            .build()
            .await?;
        Ok(Procs {
            conn,
            hub: hub.to_string(),
            handlers,
        })
    }

    pub fn connection(&self) -> &Connection {
        &self.conn
    }

    pub fn hub(&self) -> &str {
        &self.hub
    }

    /// Provide `name`. `schema` is `""` or `{"args": ..., "result": ...}` JSON Schemas.
    pub async fn provide(&self, name: &str, schema: &str, handler: FunctionHandler) -> Result<()> {
        self.handlers
            .lock()
            .expect("handlers lock poisoned")
            .insert(name.to_string(), handler);
        let provided = self
            .conn
            .call_method(
                Some(self.hub.as_str()),
                FUNCTIONS_PATH,
                Some(FUNCTIONS_IFACE),
                "Provide",
                &(name, schema),
            )
            .await;
        if let Err(e) = provided {
            self.handlers
                .lock()
                .expect("handlers lock poisoned")
                .remove(name);
            return Err(e.into());
        }
        Ok(())
    }

    pub async fn withdraw(&self, name: &str) -> Result<()> {
        self.conn
            .call_method(
                Some(self.hub.as_str()),
                FUNCTIONS_PATH,
                Some(FUNCTIONS_IFACE),
                "Withdraw",
                &(name,),
            )
            .await?;
        self.handlers
            .lock()
            .expect("handlers lock poisoned")
            .remove(name);
        Ok(())
    }
}

/// Call function `name` through the hub at `hub`. `timeout` defaults to 25 s on the hub.
pub async fn call(
    conn: &Connection,
    hub: &str,
    name: &str,
    args: &Json,
    timeout: Option<Duration>,
) -> Result<Json> {
    call_provider(conn, hub, name, None, args, timeout).await
}

/// Like [`call`], but with `provider` (a unique name from [`providers`]) only that provider
/// is tried. Apps use it to ask every provider of `name`, not just the best one.
pub async fn call_provider(
    conn: &Connection,
    hub: &str,
    name: &str,
    provider: Option<&str>,
    args: &Json,
    timeout: Option<Duration>,
) -> Result<Json> {
    let mut options = json!({});
    if let Some(t) = timeout {
        options["timeout"] = json!(t.as_secs_f64());
    }
    if let Some(p) = provider {
        options["provider"] = json!(p);
    }
    let reply = conn
        .call_method(
            Some(hub),
            FUNCTIONS_PATH,
            Some(FUNCTIONS_IFACE),
            "Call",
            &(name, json_to_dict(args)?, json_to_dict(&options)?),
        )
        .await;
    let reply = match reply {
        Ok(r) => r,
        Err(zbus::Error::MethodError(err, message, _)) => {
            return Err(Error::Call {
                name: err.to_string(),
                message: message.unwrap_or_default(),
            });
        }
        Err(zbus::Error::FDO(e)) => {
            use zbus::DBusError;
            let message = e.description().unwrap_or_default().to_string();
            return Err(Error::Call {
                name: e.name().to_string(),
                message,
            });
        }
        Err(e) => return Err(e.into()),
    };
    let result: Dict = reply.body().deserialize()?;
    dict_to_json(&result)
}

/// Every function with at least one provider.
pub async fn functions(conn: &Connection, hub: &str) -> Result<Vec<String>> {
    let reply = conn
        .call_method(
            Some(hub),
            FUNCTIONS_PATH,
            Some(FUNCTIONS_IFACE),
            "Functions",
            &(),
        )
        .await?;
    Ok(reply.body().deserialize()?)
}

/// The providers of `name`, each with its stats (calls, failures, latency_ms, pinned, schema).
pub async fn providers(conn: &Connection, hub: &str, name: &str) -> Result<Vec<(String, Json)>> {
    let reply = conn
        .call_method(
            Some(hub),
            FUNCTIONS_PATH,
            Some(FUNCTIONS_IFACE),
            "Providers",
            &(name,),
        )
        .await?;
    let list: Vec<(String, Dict)> = reply.body().deserialize()?;
    list.into_iter()
        .map(|(o, s)| Ok((o, dict_to_json(&s)?)))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pins_file() {
        let dir = std::env::temp_dir().join(format!("agentbus-pins-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("functions.conf");
        std::fs::write(
            &path,
            "# pins\nweather.current = org.example.Weather  # mine\n\n",
        )
        .unwrap();
        let pins = load_pins(&path).unwrap();
        assert_eq!(pins["weather.current"], "org.example.Weather");
        std::fs::write(&path, "weather.current\n").unwrap();
        assert!(load_pins(&path).is_err());
        assert!(load_pins(&dir.join("missing.conf")).unwrap().is_empty());
    }

    #[test]
    fn schemas() {
        assert!(Schema::parse("").unwrap().args.is_none());
        let s = Schema::parse(r#"{"args": {"type": "object", "required": ["place"]}}"#).unwrap();
        assert!(check(&s.args, &json!({"place": "Berlin"})).is_ok());
        assert!(check(&s.args, &json!({})).is_err());
        assert!(Schema::parse(r#"{"argz": {}}"#).is_err());
        assert!(Schema::parse("[1]").is_err());
    }
}
