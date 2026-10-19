//! libagentbus: tasks as the unit of work on D-Bus.
//!
//! An [`Agent`] owns a bus name and serves `org.agentbus.Agent1` at [`AGENT_PATH`]. Callers
//! [`submit`] a [`TaskSpec`] and get back a [`RemoteTask`] that streams `org.agentbus.Task1`
//! signals (progress, questions, logs, finished). A [`router`] owns a capability slot such as
//! `org.agentbus.cap.Plan1` and forwards each task to one of the agents registered with it.
//! [`topics`] is the app-domain side: apps subscribe to topics like `weather.current` on a hub,
//! and whichever service publishes them feeds the subscribers.

mod agent;
mod client;
mod error;
pub mod router;
mod task;
pub mod topics;
mod value;

pub use agent::{Agent, Handler, handler};
pub use client::{RemoteTask, TaskEvent, TaskHandle, TaskResult, submit};
pub use error::{Error, Result};
pub use task::{Outcome, TaskContext, TaskSpec, TaskState};
pub use value::{Dict, dict_to_json, json_to_dict};

pub use zbus;

/// Object path every agent (and router) serves `org.agentbus.Agent1` at. Tasks live below it.
pub const AGENT_PATH: &str = "/org/agentbus/Agent";
pub const AGENT_IFACE: &str = "org.agentbus.Agent1";
pub const TASK_IFACE: &str = "org.agentbus.Task1";
pub const ROUTER_IFACE: &str = "org.agentbus.Router1";
