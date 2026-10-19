//! The task object (`org.agentbus.Task1`) and the context a handler drives it through.

use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};

use serde_json::{Value as Json, json};
use tokio::sync::{oneshot, watch};
use zbus::{interface, object_server::InterfaceRef, object_server::SignalEmitter};

use crate::{Dict, Error, Result, dict_to_json, json_to_dict};

/// Task lifecycle states, named as in A2A.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TaskState {
    Submitted,
    Working,
    InputRequired,
    Completed,
    Failed,
    Rejected,
    Canceled,
}

impl TaskState {
    pub fn as_str(self) -> &'static str {
        match self {
            TaskState::Submitted => "submitted",
            TaskState::Working => "working",
            TaskState::InputRequired => "input-required",
            TaskState::Completed => "completed",
            TaskState::Failed => "failed",
            TaskState::Rejected => "rejected",
            TaskState::Canceled => "canceled",
        }
    }

    pub fn parse(s: &str) -> Result<Self> {
        Ok(match s {
            "submitted" => TaskState::Submitted,
            "working" => TaskState::Working,
            "input-required" => TaskState::InputRequired,
            "completed" => TaskState::Completed,
            "failed" => TaskState::Failed,
            "rejected" => TaskState::Rejected,
            "canceled" => TaskState::Canceled,
            other => return Err(Error::Task(format!("unknown task state {other:?}"))),
        })
    }

    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            TaskState::Completed | TaskState::Failed | TaskState::Rejected | TaskState::Canceled
        )
    }
}

/// What a caller submits. Travels as `a{sv}` with keys `class`, `goal`, `params`, `constraints`.
#[derive(Clone, Debug, PartialEq)]
pub struct TaskSpec {
    /// Task class, e.g. `dev.agentbus.task.plan`. Empty for free-form tasks.
    pub class: String,
    /// What the caller actually wants, in plain language.
    pub goal: String,
    /// Typed inputs for the class, as a JSON object.
    pub params: Json,
    /// Deadline, budget, maxDepth, ... as a JSON object. Opaque to the core for now.
    pub constraints: Json,
}

impl TaskSpec {
    pub fn new(class: impl Into<String>, goal: impl Into<String>) -> Self {
        TaskSpec {
            class: class.into(),
            goal: goal.into(),
            params: json!({}),
            constraints: json!({}),
        }
    }

    pub fn params(mut self, params: Json) -> Self {
        self.params = params;
        self
    }

    pub fn to_json(&self) -> Json {
        json!({"class": self.class, "goal": self.goal, "params": self.params,
               "constraints": self.constraints})
    }

    pub fn to_dict(&self) -> Result<Dict> {
        json_to_dict(&self.to_json())
    }

    pub fn from_dict(dict: &Dict) -> Result<Self> {
        Self::from_json(&dict_to_json(dict)?)
    }

    /// Parse and validate a spec. Unknown keys and wrongly typed fields are rejected.
    pub fn from_json(json: &Json) -> Result<Self> {
        let invalid = |m: String| Error::Task(format!("invalid TaskSpec: {m}"));
        let map = json
            .as_object()
            .ok_or_else(|| invalid("not an object".into()))?;
        if let Some(k) = map
            .keys()
            .find(|k| !["class", "goal", "params", "constraints"].contains(&k.as_str()))
        {
            return Err(invalid(format!("unknown key {k:?}")));
        }
        let string = |k: &str| match map.get(k) {
            None => Ok(String::new()),
            Some(Json::String(s)) => Ok(s.clone()),
            Some(_) => Err(invalid(format!("{k} must be a string"))),
        };
        let object = |k: &str| match map.get(k) {
            None => Ok(json!({})),
            Some(v @ Json::Object(_)) => Ok(v.clone()),
            Some(_) => Err(invalid(format!("{k} must be a dictionary"))),
        };
        let spec = TaskSpec {
            class: string("class")?,
            goal: string("goal")?,
            params: object("params")?,
            constraints: object("constraints")?,
        };
        if spec.goal.is_empty() && spec.class.is_empty() {
            return Err(invalid("needs a class or a goal".into()));
        }
        Ok(spec)
    }
}

/// How a handler ends a task.
#[derive(Clone, Debug, PartialEq)]
pub enum Outcome {
    /// Done; the deliverable is a JSON object.
    Completed(Json),
    /// Tried and failed.
    Failed(String),
    /// Declined: out of scope, budget too small, missing grant.
    Rejected(String),
}

struct Inner {
    state: TaskState,
    owner: String,
    deliverable: Json,
    reason: String,
    pending: HashMap<String, oneshot::Sender<Json>>,
    next_question: u64,
}

/// State shared between the exported task object and its handler.
pub(crate) struct Shared {
    spec: TaskSpec,
    inner: Mutex<Inner>,
    cancel: watch::Sender<bool>,
}

impl Shared {
    pub(crate) fn new(spec: TaskSpec, owner: String) -> Arc<Self> {
        Arc::new(Shared {
            spec,
            inner: Mutex::new(Inner {
                state: TaskState::Submitted,
                owner,
                deliverable: json!({}),
                reason: String::new(),
                pending: HashMap::new(),
                next_question: 1,
            }),
            cancel: watch::channel(false).0,
        })
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().expect("task state lock poisoned")
    }

    pub(crate) fn state(&self) -> TaskState {
        self.lock().state
    }

    /// Move to a terminal state. Returns false if the task had already finished.
    fn finish(&self, state: TaskState, deliverable: Json, reason: String) -> bool {
        let mut inner = self.lock();
        if inner.state.is_terminal() {
            return false;
        }
        inner.state = state;
        inner.deliverable = deliverable;
        inner.reason = reason;
        // Dropping the senders wakes every `ask` still waiting, with an error.
        inner.pending.clear();
        true
    }
}

/// `org.agentbus.Task1`, exported at `<AGENT_PATH>/task/<n>`.
pub(crate) struct TaskObject {
    pub(crate) shared: Arc<Shared>,
}

#[interface(name = "org.agentbus.Task1")]
impl TaskObject {
    #[zbus(property)]
    fn state(&self) -> String {
        self.shared.state().as_str().to_string()
    }

    #[zbus(property)]
    fn class(&self) -> String {
        self.shared.spec.class.clone()
    }

    #[zbus(property)]
    fn goal(&self) -> String {
        self.shared.spec.goal.clone()
    }

    #[zbus(property)]
    fn owner(&self) -> String {
        self.shared.lock().owner.clone()
    }

    /// Cancel the task. The handler is dropped, or sees `TaskContext::is_cancelled`.
    async fn cancel(
        &self,
        reason: String,
        #[zbus(signal_emitter)] emitter: SignalEmitter<'_>,
    ) -> zbus::fdo::Result<()> {
        if !self.shared.finish(TaskState::Canceled, json!({}), reason) {
            return Err(zbus::fdo::Error::Failed("task already finished".into()));
        }
        self.shared.cancel.send_replace(true);
        self.state_changed(&emitter).await?;
        Self::finished(&emitter, TaskState::Canceled.as_str()).await?;
        Ok(())
    }

    /// Answer a question the agent asked with the `Question` signal.
    async fn respond(&self, question_id: String, answer: Dict) -> zbus::fdo::Result<()> {
        let answer = dict_to_json(&answer)?;
        let sender = self.shared.lock().pending.remove(&question_id);
        let sender = sender.ok_or_else(|| {
            zbus::fdo::Error::InvalidArgs(format!("no open question {question_id:?}"))
        })?;
        // The asker can only be gone if the task finished in between.
        sender
            .send(answer)
            .map_err(|_| zbus::fdo::Error::Failed("task already finished".into()))
    }

    /// Final output: `(deliverable, report)`. The report carries `state` and, when the task
    /// did not complete, `reason`.
    #[zbus(name = "Result")]
    fn result(&self) -> zbus::fdo::Result<(Dict, Dict)> {
        let inner = self.shared.lock();
        if !inner.state.is_terminal() {
            return Err(zbus::fdo::Error::Failed(format!(
                "task is still {}",
                inner.state.as_str()
            )));
        }
        let mut report = json!({"state": inner.state.as_str()});
        if !inner.reason.is_empty() {
            report["reason"] = json!(inner.reason);
        }
        Ok((json_to_dict(&inner.deliverable)?, json_to_dict(&report)?))
    }

    #[zbus(signal)]
    async fn progress(emitter: &SignalEmitter<'_>, pct: f64, note: &str) -> zbus::Result<()>;

    #[zbus(signal)]
    async fn question(
        emitter: &SignalEmitter<'_>,
        id: &str,
        text: &str,
        schema: &str,
    ) -> zbus::Result<()>;

    #[zbus(signal)]
    async fn log(emitter: &SignalEmitter<'_>, level: &str, msg: &str) -> zbus::Result<()>;

    #[zbus(signal)]
    async fn finished(emitter: &SignalEmitter<'_>, state: &str) -> zbus::Result<()>;
}

/// The handle a task handler uses to report progress, ask questions and watch for cancel.
#[derive(Clone)]
pub struct TaskContext {
    shared: Arc<Shared>,
    iface: InterfaceRef<TaskObject>,
}

impl TaskContext {
    pub(crate) fn new(shared: Arc<Shared>, iface: InterfaceRef<TaskObject>) -> Self {
        TaskContext { shared, iface }
    }

    pub fn spec(&self) -> &TaskSpec {
        &self.shared.spec
    }

    pub fn connection(&self) -> &zbus::Connection {
        self.iface.signal_emitter().connection()
    }

    pub fn path(&self) -> String {
        self.iface.signal_emitter().path().to_string()
    }

    pub fn state(&self) -> TaskState {
        self.shared.state()
    }

    pub fn is_cancelled(&self) -> bool {
        *self.shared.cancel.borrow()
    }

    /// Resolves once the task is canceled.
    pub async fn cancelled(&self) {
        let mut rx = self.shared.cancel.subscribe();
        // The sender lives in `shared`, which we hold, so this can't fail.
        let _ = rx.wait_for(|c| *c).await;
    }

    pub async fn progress(&self, pct: f64, note: &str) -> Result<()> {
        self.check_live()?;
        TaskObject::progress(self.iface.signal_emitter(), pct, note).await?;
        Ok(())
    }

    pub async fn log(&self, level: &str, msg: &str) -> Result<()> {
        self.check_live()?;
        TaskObject::log(self.iface.signal_emitter(), level, msg).await?;
        Ok(())
    }

    /// Ask the caller a question and wait for their `Respond`. The task is `input-required`
    /// while waiting. Fails if the task is canceled meanwhile.
    pub async fn ask(&self, text: &str, schema: &str) -> Result<Json> {
        let id = {
            let mut inner = self.shared.lock();
            let id = format!("q{}", inner.next_question);
            inner.next_question += 1;
            id
        };
        self.ask_with_id(&id, text, schema).await
    }

    /// Like `ask`, under an id chosen by the caller (the router reuses the remote task's ids).
    pub(crate) async fn ask_with_id(&self, id: &str, text: &str, schema: &str) -> Result<Json> {
        let rx = {
            let mut inner = self.shared.lock();
            if inner.state.is_terminal() {
                return Err(Error::Task(format!("task is {}", inner.state.as_str())));
            }
            if inner.pending.contains_key(id) {
                return Err(Error::Task(format!("question {id:?} is already open")));
            }
            let (tx, rx) = oneshot::channel();
            inner.pending.insert(id.to_string(), tx);
            rx
        };
        self.set_state(TaskState::InputRequired).await?;
        TaskObject::question(self.iface.signal_emitter(), id, text, schema).await?;
        let answer = rx
            .await
            .map_err(|_| Error::Task(format!("task is {}", self.state().as_str())))?;
        let still_waiting = !self.shared.lock().pending.is_empty();
        if !still_waiting {
            self.set_state(TaskState::Working).await?;
        }
        Ok(answer)
    }

    fn check_live(&self) -> Result<()> {
        let state = self.state();
        if state.is_terminal() {
            return Err(Error::Task(format!("task is {}", state.as_str())));
        }
        Ok(())
    }

    pub(crate) async fn set_state(&self, state: TaskState) -> Result<()> {
        {
            let mut inner = self.shared.lock();
            if inner.state.is_terminal() {
                return Err(Error::Task(format!("task is {}", inner.state.as_str())));
            }
            inner.state = state;
        }
        self.iface
            .get()
            .await
            .state_changed(self.iface.signal_emitter())
            .await?;
        Ok(())
    }

    /// Record the handler's outcome and emit `Finished`. A no-op if the task was canceled.
    pub(crate) async fn finish(&self, outcome: Outcome) -> Result<()> {
        let (state, deliverable, reason) = match outcome {
            Outcome::Completed(d) => {
                // The deliverable must be representable as a{sv}; fail the task otherwise.
                match json_to_dict(&d) {
                    Ok(_) => (TaskState::Completed, d, String::new()),
                    Err(e) => (
                        TaskState::Failed,
                        json!({}),
                        format!("bad deliverable: {e}"),
                    ),
                }
            }
            Outcome::Failed(r) => (TaskState::Failed, json!({}), r),
            Outcome::Rejected(r) => (TaskState::Rejected, json!({}), r),
        };
        if !self.shared.finish(state, deliverable, reason) {
            return Ok(());
        }
        let emitter = self.iface.signal_emitter();
        self.iface.get().await.state_changed(emitter).await?;
        TaskObject::finished(emitter, state.as_str()).await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spec_round_trip_and_validation() {
        let spec =
            TaskSpec::new("dev.agentbus.task.plan", "plan a trip").params(json!({"days": 3}));
        assert_eq!(TaskSpec::from_dict(&spec.to_dict().unwrap()).unwrap(), spec);
        assert!(TaskSpec::from_json(&json!({"goal": "x", "bogus": 1})).is_err());
        assert!(TaskSpec::from_json(&json!({"goal": 1})).is_err());
        assert!(TaskSpec::from_json(&json!({})).is_err());
    }
}
