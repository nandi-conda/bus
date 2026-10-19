//! A capability slot router: owns a slot name like `org.agentbus.cap.Plan1`, keeps a list of
//! member agents (each registers with `org.agentbus.Router1.Register`), and forwards each
//! submitted task to the next live member, mirroring the member task's signals on its own task.

use std::sync::{Arc, Mutex};

use zbus::{Connection, fdo::DBusProxy, interface, message::Header, names::BusName};

use crate::{AGENT_PATH, Agent, Outcome, TaskContext, TaskEvent, TaskState, handler, submit};

type Members = Arc<Mutex<Vec<String>>>;

struct RouterObject {
    members: Members,
}

#[interface(name = "org.agentbus.Router1")]
impl RouterObject {
    /// Add the calling peer to this slot. The member is the caller's own unique name.
    fn register(&self, #[zbus(header)] header: Header<'_>) -> zbus::fdo::Result<()> {
        let sender = header
            .sender()
            .ok_or_else(|| zbus::fdo::Error::Failed("call has no sender".into()))?
            .to_string();
        let mut members = self.members.lock().expect("members lock poisoned");
        if !members.contains(&sender) {
            members.push(sender);
        }
        Ok(())
    }

    fn unregister(&self, #[zbus(header)] header: Header<'_>) {
        if let Some(sender) = header.sender() {
            self.members
                .lock()
                .expect("members lock poisoned")
                .retain(|m| m != sender.as_str());
        }
    }

    #[zbus(property)]
    fn members(&self) -> Vec<String> {
        self.members.lock().expect("members lock poisoned").clone()
    }
}

/// Take `slot` on the session bus and route its tasks to registered members.
pub async fn serve(slot: &str) -> crate::Result<Agent> {
    let members: Members = Default::default();
    let routing = members.clone();
    let agent = Agent::serve(
        slot,
        Vec::new(),
        handler(move |ctx| {
            let members = routing.clone();
            async move {
                forward(ctx, members)
                    .await
                    .unwrap_or_else(|e| Outcome::Failed(e.to_string()))
            }
        }),
    )
    .await?;
    agent
        .connection()
        .object_server()
        .at(AGENT_PATH, RouterObject { members })
        .await?;
    Ok(agent)
}

/// Pick the next live member, round-robin, dropping members that left the bus.
async fn pick(conn: &Connection, members: &Members) -> crate::Result<Option<String>> {
    let dbus = DBusProxy::new(conn).await?;
    loop {
        let candidate = {
            let mut m = members.lock().expect("members lock poisoned");
            if m.is_empty() {
                return Ok(None);
            }
            let first = m.remove(0);
            m.push(first.clone());
            first
        };
        if dbus
            .name_has_owner(BusName::try_from(candidate.as_str())?)
            .await?
        {
            return Ok(Some(candidate));
        }
        members
            .lock()
            .expect("members lock poisoned")
            .retain(|m| *m != candidate);
    }
}

async fn forward(ctx: TaskContext, members: Members) -> crate::Result<Outcome> {
    let conn = ctx.connection().clone();
    let Some(member) = pick(&conn, &members).await? else {
        return Ok(Outcome::Rejected("no agent has joined this slot".into()));
    };
    let mut remote = submit(&conn, &member, ctx.spec()).await?;
    ctx.log(
        "info",
        &format!("routed to {member} at {}", remote.handle().path()),
    )
    .await?;

    // Cancel travels down even though the runner drops this future on cancel.
    let canceller = {
        let (ctx, handle) = (ctx.clone(), remote.handle().clone());
        tokio::spawn(async move {
            ctx.cancelled().await;
            let _ = handle.cancel("canceled by caller").await;
        })
    };

    let outcome = loop {
        match remote.next_event().await? {
            Some(TaskEvent::Progress { pct, note }) => ctx.progress(pct, &note).await?,
            Some(TaskEvent::Log { level, msg }) => ctx.log(&level, &msg).await?,
            Some(TaskEvent::Question { id, text, schema }) => {
                // Re-raise under the same id; relay the answer when the caller responds.
                let (ctx, handle) = (ctx.clone(), remote.handle().clone());
                tokio::spawn(async move {
                    if let Ok(answer) = ctx.ask_with_id(&id, &text, &schema).await {
                        let _ = handle.respond(&id, &answer).await;
                    }
                });
            }
            Some(TaskEvent::Finished(_)) | None => {
                let result = remote.handle().result().await?;
                let reason = result.report["reason"]
                    .as_str()
                    .unwrap_or_default()
                    .to_string();
                break match result.state {
                    TaskState::Completed => Outcome::Completed(result.deliverable),
                    TaskState::Rejected => Outcome::Rejected(reason),
                    _ => Outcome::Failed(reason),
                };
            }
        }
    };
    canceller.abort();
    Ok(outcome)
}
