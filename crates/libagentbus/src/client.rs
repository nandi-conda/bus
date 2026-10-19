//! The caller's side: submit a task and follow it.

use std::num::NonZeroUsize;

use futures_util::StreamExt;
use serde_json::Value as Json;
use zbus::{
    Connection, MatchRule, MessageStream, OwnedObjectPath, fdo::DBusProxy, message, names::BusName,
};

use crate::{
    AGENT_IFACE, AGENT_PATH, Dict, Error, Result, TASK_IFACE, TaskSpec, TaskState, dict_to_json,
    json_to_dict,
};

/// A signal from a task.
#[derive(Clone, Debug, PartialEq)]
pub enum TaskEvent {
    Progress {
        pct: f64,
        note: String,
    },
    Question {
        id: String,
        text: String,
        schema: String,
    },
    Log {
        level: String,
        msg: String,
    },
    Finished(TaskState),
}

/// What `Result()` returns once the task is terminal.
#[derive(Clone, Debug, PartialEq)]
pub struct TaskResult {
    pub state: TaskState,
    pub deliverable: Json,
    pub report: Json,
}

/// Cheap, cloneable handle for calling methods on a remote task.
#[derive(Clone, Debug)]
pub struct TaskHandle {
    conn: Connection,
    owner: String,
    path: OwnedObjectPath,
}

impl TaskHandle {
    pub fn owner(&self) -> &str {
        &self.owner
    }

    pub fn path(&self) -> &str {
        self.path.as_str()
    }

    async fn call<B>(&self, method: &str, body: &B) -> Result<message::Message>
    where
        B: serde::ser::Serialize + zbus::DynamicType,
    {
        Ok(self
            .conn
            .call_method(
                Some(self.owner.as_str()),
                &self.path,
                Some(TASK_IFACE),
                method,
                body,
            )
            .await?)
    }

    pub async fn respond(&self, question_id: &str, answer: &Json) -> Result<()> {
        self.call("Respond", &(question_id, json_to_dict(answer)?))
            .await?;
        Ok(())
    }

    pub async fn cancel(&self, reason: &str) -> Result<()> {
        self.call("Cancel", &(reason,)).await?;
        Ok(())
    }

    pub async fn result(&self) -> Result<TaskResult> {
        let reply = self.call("Result", &()).await?;
        let (deliverable, report): (Dict, Dict) = reply.body().deserialize()?;
        let report = dict_to_json(&report)?;
        let state = report["state"]
            .as_str()
            .ok_or_else(|| Error::Task("Result report has no state".into()))?;
        Ok(TaskResult {
            state: TaskState::parse(state)?,
            deliverable: dict_to_json(&deliverable)?,
            report,
        })
    }

    pub async fn state(&self) -> Result<TaskState> {
        let reply = self
            .conn
            .call_method(
                Some(self.owner.as_str()),
                &self.path,
                Some("org.freedesktop.DBus.Properties"),
                "Get",
                &(TASK_IFACE, "State"),
            )
            .await?;
        let value: zbus::OwnedVariant = reply.body().deserialize()?;
        let state: String = value
            .try_into()
            .map_err(|e: zbus::Error| Error::Value(e.to_string()))?;
        TaskState::parse(&state)
    }
}

/// A submitted task and the stream of its signals.
pub struct RemoteTask {
    handle: TaskHandle,
    stream: MessageStream,
    done: bool,
}

/// Submit `spec` to the agent or capability slot at `dest`.
pub async fn submit(conn: &Connection, dest: &str, spec: &TaskSpec) -> Result<RemoteTask> {
    // Resolve the owner first and subscribe before submitting, so no early signal is missed.
    let dbus = DBusProxy::new(conn).await?;
    let owner = dbus
        .get_name_owner(BusName::try_from(dest)?)
        .await?
        .to_string();
    let rule = MatchRule::builder()
        .msg_type(message::Type::Signal)
        .sender(owner.as_str())
        .path_namespace(AGENT_PATH)
        .build()?;
    let stream = MessageStream::for_match_rule(rule, conn, NonZeroUsize::new(1024)).await?;
    let reply = conn
        .call_method(
            Some(owner.as_str()),
            AGENT_PATH,
            Some(AGENT_IFACE),
            "Submit",
            &(spec.to_dict()?,),
        )
        .await?;
    let path: OwnedObjectPath = reply.body().deserialize()?;
    Ok(RemoteTask {
        handle: TaskHandle {
            conn: conn.clone(),
            owner,
            path,
        },
        stream,
        done: false,
    })
}

impl RemoteTask {
    pub fn handle(&self) -> &TaskHandle {
        &self.handle
    }

    /// The next signal from this task, or `None` after `Finished`.
    pub async fn next_event(&mut self) -> Result<Option<TaskEvent>> {
        while !self.done {
            let Some(msg) = self.stream.next().await else {
                return Err(Error::Task("bus connection closed".into()));
            };
            let msg = msg?;
            let header = msg.header();
            let ours = header
                .path()
                .is_some_and(|p| p.as_str() == self.handle.path.as_str())
                && header.interface().is_some_and(|i| i.as_str() == TASK_IFACE);
            if !ours {
                continue;
            }
            let body = msg.body();
            let event = match header.member().map(|m| m.as_str()) {
                Some("Progress") => {
                    let (pct, note): (f64, String) = body.deserialize()?;
                    TaskEvent::Progress { pct, note }
                }
                Some("Question") => {
                    let (id, text, schema): (String, String, String) = body.deserialize()?;
                    TaskEvent::Question { id, text, schema }
                }
                Some("Log") => {
                    let (level, msg): (String, String) = body.deserialize()?;
                    TaskEvent::Log { level, msg }
                }
                Some("Finished") => {
                    let (state,): (String,) = body.deserialize()?;
                    self.done = true;
                    TaskEvent::Finished(TaskState::parse(&state)?)
                }
                _ => continue,
            };
            return Ok(Some(event));
        }
        Ok(None)
    }
}
