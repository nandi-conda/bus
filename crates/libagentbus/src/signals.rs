//! Broadcast signals: plain D-Bus signals with no destination, for request/response broadcasts
//! such as an app's `GetFiles(s request)` answered by every provider's
//! `SendFiles(s request, s provider, as files)`. Nobody needs to know who else is on the bus.
//!
//! Arguments are JSON values typed the way D-Bus apps expect: strings `s`, integers `x`,
//! floats `d`, booleans `b`, lists of strings `as` (an empty list is `as` too), lists of
//! integers `ax`, other lists `av`, objects `a{sv}`.

use std::num::NonZeroUsize;

use futures_util::StreamExt;
use serde_json::Value as Json;
use zbus::{Connection, MatchRule, MessageStream, Structure, Variant, message};

use crate::{Error, Result, value};

fn to_arg(json: &Json) -> Result<Variant<'static>> {
    if let Json::Array(items) = json {
        if items.iter().all(Json::is_string) {
            let strings: Vec<String> = items
                .iter()
                .map(|s| s.as_str().unwrap_or_default().to_string())
                .collect();
            return Ok(Variant::from(strings));
        }
        if items.iter().all(Json::is_i64) {
            let ints: Vec<i64> = items.iter().filter_map(Json::as_i64).collect();
            return Ok(Variant::from(ints));
        }
    }
    value::to_variant(json)
}

/// Emit `iface.member(args...)` from `path` to everyone listening.
pub async fn broadcast(
    conn: &Connection,
    path: &str,
    iface: &str,
    member: &str,
    args: &[Json],
) -> Result<()> {
    if args.is_empty() {
        conn.emit_signal(None::<&str>, path, iface, member, &())
            .await?;
        return Ok(());
    }
    let mut body = Structure::builder();
    for arg in args {
        body = body.append_field(to_arg(arg)?);
    }
    let body = body.build().map_err(|e| Error::Value(e.to_string()))?;
    conn.emit_signal(None::<&str>, path, iface, member, &body)
        .await?;
    Ok(())
}

/// One received broadcast.
#[derive(Debug, Clone, PartialEq)]
pub struct Signal {
    /// Unique bus name of the sender.
    pub sender: String,
    pub path: String,
    pub interface: String,
    pub member: String,
    pub args: Vec<Json>,
}

/// Signals matching a listen rule, in the order they arrived.
pub struct Listener {
    stream: MessageStream,
}

/// Listen for signals on `path` and `iface`, optionally only `member`. Start listening before
/// broadcasting a request, so no reply is missed.
pub async fn listen(
    conn: &Connection,
    path: &str,
    iface: &str,
    member: Option<&str>,
) -> Result<Listener> {
    let mut rule = MatchRule::builder()
        .msg_type(message::Type::Signal)
        .path(path)
        .interface(iface);
    if let Some(m) = member {
        rule = rule.member(m);
    }
    let stream =
        MessageStream::for_match_rule(rule.build()?, conn, NonZeroUsize::new(1024)).await?;
    Ok(Listener { stream })
}

impl Listener {
    pub async fn next_signal(&mut self) -> Result<Signal> {
        let Some(msg) = self.stream.next().await else {
            return Err(Error::Closed("bus connection closed".into()));
        };
        let msg = msg?;
        let header = msg.header();
        let text = |s: Option<&str>| s.unwrap_or_default().to_string();
        let body = msg.body();
        let args = if body.signature().to_string().is_empty() {
            Vec::new()
        } else {
            let fields: Structure<'_> = body.deserialize()?;
            fields
                .fields()
                .iter()
                .map(value::to_json)
                .collect::<Result<_>>()?
        };
        Ok(Signal {
            sender: text(header.sender().map(|s| s.as_str())),
            path: text(header.path().map(|p| p.as_str())),
            interface: text(header.interface().map(|i| i.as_str())),
            member: text(header.member().map(|m| m.as_str())),
            args,
        })
    }
}
