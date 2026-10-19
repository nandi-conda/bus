//! App-domain subscriptions: apps subscribe to topics such as `weather.current` for a subject such
//! as `Berlin`, and whatever service or agent publishes that topic feeds them through a hub.
//!
//! The hub owns `dev.agentbus.Topics` and serves `dev.agentbus.Topics1` at [`TOPICS_PATH`]:
//! - `Subscribe(s pattern, s subject) -> o` creates a `dev.agentbus.Subscription1` object owned by
//!   the caller. Its `Event(s topic, s subject, a{sv} payload, t seq)` signals go to the caller
//!   only. Retained values matching the subscription are replayed straight away. The subscription
//!   goes away on `Unsubscribe()` or when the caller leaves the bus.
//! - `Publish(s topic, s subject, a{sv} payload) -> u` fans an event out to matching subscribers
//!   and retains it as the topic's current value for that subject.
//! - `Demand() -> a(ssu)` and the `DemandChanged(s pattern, s subject, u subscribers)` signal tell
//!   publishers what is being watched, so a weather source only polls the places someone watches.
//!
//! Patterns are a topic (`weather.current`), a prefix (`weather.*`) or `*`. An empty subject
//! matches every subject.

use std::{
    collections::{BTreeMap, HashMap},
    num::NonZeroUsize,
    sync::{Arc, Mutex},
};

use futures_util::StreamExt;
use serde_json::Value as Json;
use zbus::{
    Connection, MatchRule, MessageStream, OwnedObjectPath, connection,
    fdo::DBusProxy,
    interface,
    message::{self, Header},
    names::BusName,
};

use crate::{Dict, Error, Result, dict_to_json, json_to_dict};

pub const TOPICS_NAME: &str = "dev.agentbus.Topics";
pub const TOPICS_PATH: &str = "/dev/agentbus/Topics";
pub const TOPICS_IFACE: &str = "dev.agentbus.Topics1";
pub const SUBSCRIPTION_IFACE: &str = "dev.agentbus.Subscription1";

/// Does `pattern` (`a.b`, `a.*` or `*`) cover `topic`?
pub fn pattern_matches(pattern: &str, topic: &str) -> bool {
    match pattern.strip_suffix('*') {
        Some("") => true,
        Some(prefix) => prefix.ends_with('.') && topic.starts_with(prefix),
        None => pattern == topic,
    }
}

fn subject_matches(wanted: &str, subject: &str) -> bool {
    wanted.is_empty() || wanted == subject
}

fn check_pattern(pattern: &str) -> zbus::fdo::Result<()> {
    let body = pattern
        .strip_suffix(".*")
        .unwrap_or(if pattern == "*" { "x" } else { pattern });
    check_topic(body)
}

fn check_topic(topic: &str) -> zbus::fdo::Result<()> {
    let ok = !topic.is_empty()
        && topic.split('.').all(|part| {
            !part.is_empty()
                && part
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        });
    if ok {
        Ok(())
    } else {
        Err(zbus::fdo::Error::InvalidArgs(format!(
            "bad topic {topic:?}: use dotted words like weather.current"
        )))
    }
}

struct Sub {
    owner: String,
    pattern: String,
    subject: String,
    path: OwnedObjectPath,
}

#[derive(Default)]
struct HubState {
    next_sub: u64,
    seq: u64,
    subs: HashMap<u64, Sub>,
    /// Current value per (topic, subject).
    retained: BTreeMap<(String, String), (Json, u64)>,
}

impl HubState {
    fn demand_for(&self, pattern: &str, subject: &str) -> u32 {
        self.subs
            .values()
            .filter(|s| s.pattern == pattern && s.subject == subject)
            .count() as u32
    }
}

type State = Arc<Mutex<HubState>>;

fn lock(state: &State) -> std::sync::MutexGuard<'_, HubState> {
    state.lock().expect("topics state lock poisoned")
}

struct HubObject {
    state: State,
}

#[interface(name = "dev.agentbus.Topics1")]
impl HubObject {
    async fn subscribe(
        &self,
        pattern: String,
        subject: String,
        #[zbus(header)] header: Header<'_>,
        #[zbus(connection)] conn: &Connection,
    ) -> zbus::fdo::Result<OwnedObjectPath> {
        check_pattern(&pattern)?;
        let owner = header
            .sender()
            .ok_or_else(|| zbus::fdo::Error::Failed("call has no sender".into()))?
            .to_string();
        let (id, path, replay, count) = {
            let mut st = lock(&self.state);
            st.next_sub += 1;
            let id = st.next_sub;
            let path = OwnedObjectPath::try_from(format!("{TOPICS_PATH}/sub/{id}"))?;
            st.subs.insert(
                id,
                Sub {
                    owner: owner.clone(),
                    pattern: pattern.clone(),
                    subject: subject.clone(),
                    path: path.clone(),
                },
            );
            let replay: Vec<_> = st
                .retained
                .iter()
                .filter(|((t, s), _)| pattern_matches(&pattern, t) && subject_matches(&subject, s))
                .map(|((t, s), (p, seq))| (t.clone(), s.clone(), p.clone(), *seq))
                .collect();
            (id, path, replay, st.demand_for(&pattern, &subject))
        };
        conn.object_server()
            .at(
                &path,
                SubscriptionObject {
                    id,
                    pattern: pattern.clone(),
                    subject: subject.clone(),
                    state: self.state.clone(),
                },
            )
            .await?;
        // The caller listens before it subscribes, so these arrive ahead of live events.
        for (topic, subj, payload, seq) in replay {
            send_event(conn, &owner, &path, &topic, &subj, &payload, seq).await?;
        }
        emit_demand(conn, &pattern, &subject, count).await?;
        Ok(path)
    }

    /// Deliver an event to every matching subscriber and retain it. Returns how many got it.
    async fn publish(
        &self,
        topic: String,
        subject: String,
        payload: Dict,
        #[zbus(connection)] conn: &Connection,
    ) -> zbus::fdo::Result<u32> {
        check_topic(&topic)?;
        let payload = dict_to_json(&payload)?;
        let (seq, targets) = {
            let mut st = lock(&self.state);
            st.seq += 1;
            let seq = st.seq;
            st.retained
                .insert((topic.clone(), subject.clone()), (payload.clone(), seq));
            let targets: Vec<_> = st
                .subs
                .values()
                .filter(|s| {
                    pattern_matches(&s.pattern, &topic) && subject_matches(&s.subject, &subject)
                })
                .map(|s| (s.owner.clone(), s.path.clone()))
                .collect();
            (seq, targets)
        };
        for (owner, path) in &targets {
            send_event(conn, owner, path, &topic, &subject, &payload, seq).await?;
        }
        Ok(targets.len() as u32)
    }

    /// What is being watched right now: (pattern, subject, subscribers).
    fn demand(&self) -> Vec<(String, String, u32)> {
        let st = lock(&self.state);
        let mut counts: BTreeMap<(String, String), u32> = BTreeMap::new();
        for s in st.subs.values() {
            *counts
                .entry((s.pattern.clone(), s.subject.clone()))
                .or_default() += 1;
        }
        counts.into_iter().map(|((p, s), n)| (p, s, n)).collect()
    }

    /// Current retained values matching `pattern` and `subject`.
    fn retained(
        &self,
        pattern: String,
        subject: String,
    ) -> zbus::fdo::Result<Vec<(String, String, Dict)>> {
        check_pattern(&pattern)?;
        let st = lock(&self.state);
        st.retained
            .iter()
            .filter(|((t, s), _)| pattern_matches(&pattern, t) && subject_matches(&subject, s))
            .map(|((t, s), (p, _))| Ok((t.clone(), s.clone(), json_to_dict(p)?)))
            .collect::<Result<_>>()
            .map_err(Into::into)
    }

    #[zbus(signal)]
    async fn demand_changed(
        emitter: &zbus::object_server::SignalEmitter<'_>,
        pattern: &str,
        subject: &str,
        subscribers: u32,
    ) -> zbus::Result<()>;
}

struct SubscriptionObject {
    id: u64,
    pattern: String,
    subject: String,
    state: State,
}

#[interface(name = "dev.agentbus.Subscription1")]
impl SubscriptionObject {
    async fn unsubscribe(
        &self,
        #[zbus(header)] header: Header<'_>,
        #[zbus(connection)] conn: &Connection,
    ) -> zbus::fdo::Result<()> {
        let owner = lock(&self.state)
            .subs
            .get(&self.id)
            .map(|s| s.owner.clone());
        if owner.as_deref() != header.sender().map(|s| s.as_str()) {
            return Err(zbus::fdo::Error::AccessDenied(
                "only the subscriber can unsubscribe".into(),
            ));
        }
        remove_subs(conn, &self.state, |id, _| id == self.id).await?;
        Ok(())
    }

    #[zbus(property)]
    fn pattern(&self) -> String {
        self.pattern.clone()
    }

    #[zbus(property)]
    fn subject(&self) -> String {
        self.subject.clone()
    }

    #[zbus(signal)]
    async fn event(
        emitter: &zbus::object_server::SignalEmitter<'_>,
        topic: &str,
        subject: &str,
        payload: Dict,
        seq: u64,
    ) -> zbus::Result<()>;
}

async fn send_event(
    conn: &Connection,
    owner: &str,
    path: &OwnedObjectPath,
    topic: &str,
    subject: &str,
    payload: &Json,
    seq: u64,
) -> zbus::fdo::Result<()> {
    conn.emit_signal(
        Some(owner),
        path.as_str(),
        SUBSCRIPTION_IFACE,
        "Event",
        &(topic, subject, json_to_dict(payload)?, seq),
    )
    .await?;
    Ok(())
}

async fn emit_demand(conn: &Connection, pattern: &str, subject: &str, n: u32) -> zbus::Result<()> {
    conn.emit_signal(
        None::<&str>,
        TOPICS_PATH,
        TOPICS_IFACE,
        "DemandChanged",
        &(pattern, subject, n),
    )
    .await
}

/// Drop the subscriptions `keep_out` selects, remove their objects and announce new demand.
async fn remove_subs(
    conn: &Connection,
    state: &State,
    keep_out: impl Fn(u64, &Sub) -> bool,
) -> zbus::Result<()> {
    let (gone, demand) = {
        let mut st = lock(state);
        let ids: Vec<u64> = st
            .subs
            .iter()
            .filter(|(id, s)| keep_out(**id, s))
            .map(|(id, _)| *id)
            .collect();
        let gone: Vec<Sub> = ids.iter().filter_map(|id| st.subs.remove(id)).collect();
        let mut keys: Vec<(String, String)> = gone
            .iter()
            .map(|s| (s.pattern.clone(), s.subject.clone()))
            .collect();
        keys.sort();
        keys.dedup();
        let demand: Vec<_> = keys
            .into_iter()
            .map(|(p, s)| {
                let n = st.demand_for(&p, &s);
                (p, s, n)
            })
            .collect();
        (gone, demand)
    };
    for sub in &gone {
        conn.object_server()
            .remove::<SubscriptionObject, _>(&sub.path)
            .await?;
    }
    for (p, s, n) in demand {
        emit_demand(conn, &p, &s, n).await?;
    }
    Ok(())
}

/// A running topics hub. Dropping it closes its connection.
pub struct Hub {
    conn: Connection,
}

impl Hub {
    pub fn connection(&self) -> &Connection {
        &self.conn
    }
}

/// Take `bus_name` (normally [`TOPICS_NAME`]) on the session bus and serve the hub.
pub async fn serve(bus_name: &str) -> Result<Hub> {
    let state: State = Default::default();
    let conn = connection::Builder::session()
        .name(bus_name)
        .serve_at(
            TOPICS_PATH,
            HubObject {
                state: state.clone(),
            },
        )
        .build()
        .await?;

    // Subscriptions are leases: when a subscriber leaves the bus, its subscriptions go too.
    let mut gone = DBusProxy::new(&conn)
        .await?
        .receive_name_owner_changed()
        .await?;
    let watcher = conn.clone();
    tokio::spawn(async move {
        while let Some(signal) = gone.next().await {
            let Ok(args) = signal.args() else { continue };
            if args.new_owner().is_some() || !args.name().starts_with(':') {
                continue;
            }
            let name = args.name().to_string();
            if let Err(e) = remove_subs(&watcher, &state, |_, s| s.owner == name).await {
                eprintln!("agentbus topics: could not drop subscriptions of {name}: {e}");
            }
        }
    });
    Ok(Hub { conn })
}

/// One event delivered to a subscriber.
#[derive(Debug, Clone, PartialEq)]
pub struct TopicEvent {
    pub topic: String,
    pub subject: String,
    pub payload: Json,
    pub seq: u64,
}

/// A live subscription. Events stop when it is dropped with [`Subscription::unsubscribe`] or
/// when its connection closes.
pub struct Subscription {
    conn: Connection,
    hub: String,
    path: OwnedObjectPath,
    stream: MessageStream,
}

async fn owner_of(conn: &Connection, hub: &str) -> Result<String> {
    Ok(DBusProxy::new(conn)
        .await?
        .get_name_owner(BusName::try_from(hub)?)
        .await?
        .to_string())
}

/// Subscribe to `pattern` (`weather.current`, `weather.*`, `*`) for `subject` (`""` for any) on
/// the hub at `hub`.
pub async fn subscribe(
    conn: &Connection,
    hub: &str,
    pattern: &str,
    subject: &str,
) -> Result<Subscription> {
    let owner = owner_of(conn, hub).await?;
    let rule = MatchRule::builder()
        .msg_type(message::Type::Signal)
        .sender(owner.as_str())
        .interface(SUBSCRIPTION_IFACE)
        .path_namespace(TOPICS_PATH)
        .build()?;
    let stream = MessageStream::for_match_rule(rule, conn, NonZeroUsize::new(1024)).await?;
    let reply = conn
        .call_method(
            Some(owner.as_str()),
            TOPICS_PATH,
            Some(TOPICS_IFACE),
            "Subscribe",
            &(pattern, subject),
        )
        .await?;
    let path: OwnedObjectPath = reply.body().deserialize()?;
    Ok(Subscription {
        conn: conn.clone(),
        hub: owner,
        path,
        stream,
    })
}

impl Subscription {
    pub fn path(&self) -> &str {
        self.path.as_str()
    }

    /// The next event for this subscription.
    pub async fn next_event(&mut self) -> Result<TopicEvent> {
        loop {
            let Some(msg) = self.stream.next().await else {
                return Err(Error::Task("bus connection closed".into()));
            };
            let msg = msg?;
            let header = msg.header();
            let ours = header
                .path()
                .is_some_and(|p| p.as_str() == self.path.as_str())
                && header.member().is_some_and(|m| m.as_str() == "Event");
            if !ours {
                continue;
            }
            let (topic, subject, payload, seq): (String, String, Dict, u64) =
                msg.body().deserialize()?;
            return Ok(TopicEvent {
                topic,
                subject,
                payload: dict_to_json(&payload)?,
                seq,
            });
        }
    }

    pub async fn unsubscribe(self) -> Result<()> {
        self.conn
            .call_method(
                Some(self.hub.as_str()),
                self.path.as_str(),
                Some(SUBSCRIPTION_IFACE),
                "Unsubscribe",
                &(),
            )
            .await?;
        Ok(())
    }
}

/// Publish `payload` (a JSON object) on `topic` for `subject`. Returns how many subscribers got it.
pub async fn publish(
    conn: &Connection,
    hub: &str,
    topic: &str,
    subject: &str,
    payload: &Json,
) -> Result<u32> {
    let reply = conn
        .call_method(
            Some(hub),
            TOPICS_PATH,
            Some(TOPICS_IFACE),
            "Publish",
            &(topic, subject, json_to_dict(payload)?),
        )
        .await?;
    Ok(reply.body().deserialize()?)
}

/// How many subscribers watch `pattern` for `subject`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Demand {
    pub pattern: String,
    pub subject: String,
    pub subscribers: u32,
}

/// What a publisher sees: the demand when it started watching, then every change.
pub struct DemandWatch {
    pub current: Vec<Demand>,
    stream: MessageStream,
}

/// Follow demand on the hub at `hub`, so a publisher can start and stop producing topics as
/// apps subscribe and leave.
pub async fn watch_demand(conn: &Connection, hub: &str) -> Result<DemandWatch> {
    let owner = owner_of(conn, hub).await?;
    let rule = MatchRule::builder()
        .msg_type(message::Type::Signal)
        .sender(owner.as_str())
        .interface(TOPICS_IFACE)
        .member("DemandChanged")
        .path(TOPICS_PATH)
        .build()?;
    let stream = MessageStream::for_match_rule(rule, conn, NonZeroUsize::new(1024)).await?;
    let reply = conn
        .call_method(
            Some(owner.as_str()),
            TOPICS_PATH,
            Some(TOPICS_IFACE),
            "Demand",
            &(),
        )
        .await?;
    let current: Vec<(String, String, u32)> = reply.body().deserialize()?;
    Ok(DemandWatch {
        current: current
            .into_iter()
            .map(|(pattern, subject, subscribers)| Demand {
                pattern,
                subject,
                subscribers,
            })
            .collect(),
        stream,
    })
}

impl DemandWatch {
    pub async fn next_change(&mut self) -> Result<Demand> {
        let Some(msg) = self.stream.next().await else {
            return Err(Error::Task("bus connection closed".into()));
        };
        let (pattern, subject, subscribers): (String, String, u32) = msg?.body().deserialize()?;
        Ok(Demand {
            pattern,
            subject,
            subscribers,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn patterns() {
        assert!(pattern_matches("weather.current", "weather.current"));
        assert!(!pattern_matches("weather.current", "weather.alert"));
        assert!(pattern_matches("weather.*", "weather.alert"));
        assert!(!pattern_matches("weather.*", "weatherman.alert"));
        assert!(pattern_matches("*", "calendar.event.changed"));
        assert!(check_pattern("weather.*").is_ok());
        assert!(check_pattern("*").is_ok());
        assert!(check_pattern("weather.").is_err());
        assert!(check_topic("weather.*").is_err());
    }
}
