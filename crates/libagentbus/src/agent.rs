//! An agent: a bus name serving `org.agentbus.Agent1`, running a handler per submitted task.

use std::{
    future::Future,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};

use zbus::{
    Connection, OwnedObjectPath, connection, fdo::ObjectManager, interface, message::Header,
};

use crate::{
    AGENT_PATH, Dict, ROUTER_IFACE, Result,
    task::{Outcome, Shared, TaskContext, TaskObject, TaskSpec, TaskState},
};

/// Runs one task to its outcome.
pub type Handler =
    Arc<dyn Fn(TaskContext) -> Pin<Box<dyn Future<Output = Outcome> + Send>> + Send + Sync>;

/// Wrap an async closure as a [`Handler`].
pub fn handler<F, Fut>(f: F) -> Handler
where
    F: Fn(TaskContext) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Outcome> + Send + 'static,
{
    Arc::new(move |ctx| Box::pin(f(ctx)))
}

struct AgentObject {
    classes: Vec<String>,
    handler: Handler,
    next_task: AtomicU64,
    tasks: std::sync::Mutex<Vec<Arc<Shared>>>,
}

#[interface(name = "org.agentbus.Agent1")]
impl AgentObject {
    /// Start a task. Returns its object path, under the agent's own bus name.
    async fn submit(
        &self,
        spec: Dict,
        #[zbus(header)] header: Header<'_>,
        #[zbus(connection)] conn: &Connection,
    ) -> zbus::fdo::Result<OwnedObjectPath> {
        let spec = TaskSpec::from_dict(&spec)?;
        if !self.classes.is_empty() && !self.classes.contains(&spec.class) {
            return Err(zbus::fdo::Error::InvalidArgs(format!(
                "this agent does not handle task class {:?}",
                spec.class
            )));
        }
        let owner = header.sender().map(|s| s.to_string()).unwrap_or_default();
        let n = self.next_task.fetch_add(1, Ordering::Relaxed);
        let path = OwnedObjectPath::try_from(format!("{AGENT_PATH}/task/{n}"))?;
        let shared = Shared::new(spec, owner);
        let server = conn.object_server();
        server
            .at(
                &path,
                TaskObject {
                    shared: shared.clone(),
                },
            )
            .await?;
        let iface = server.interface::<_, TaskObject>(&path).await?;
        self.tasks
            .lock()
            .expect("task list lock poisoned")
            .push(shared.clone());

        let ctx = TaskContext::new(shared, iface);
        let handler = self.handler.clone();
        tokio::spawn(async move {
            if ctx.set_state(TaskState::Working).await.is_err() {
                return; // canceled before it started
            }
            let outcome = tokio::select! {
                outcome = handler(ctx.clone()) => outcome,
                _ = ctx.cancelled() => return,
            };
            if let Err(e) = ctx.finish(outcome).await {
                eprintln!("agentbus: could not finish {}: {e}", ctx.path());
            }
        });
        Ok(path)
    }

    #[zbus(property)]
    fn task_classes(&self) -> Vec<String> {
        self.classes.clone()
    }

    /// Number of tasks that have not finished yet.
    #[zbus(property)]
    fn load(&self) -> u32 {
        let tasks = self.tasks.lock().expect("task list lock poisoned");
        tasks.iter().filter(|t| !t.state().is_terminal()).count() as u32
    }

    #[zbus(property)]
    fn status(&self) -> String {
        "online".into()
    }
}

/// A running agent. Dropping it closes its connection.
pub struct Agent {
    conn: Connection,
}

impl Agent {
    /// Connect to the session bus, take `bus_name` and serve `Agent1` at [`AGENT_PATH`].
    /// An empty `classes` list accepts every task class.
    pub async fn serve(bus_name: &str, classes: Vec<String>, handler: Handler) -> Result<Agent> {
        let agent = AgentObject {
            classes,
            handler,
            next_task: AtomicU64::new(1),
            tasks: Default::default(),
        };
        let conn = connection::Builder::session()
            .name(bus_name)
            .serve_at(AGENT_PATH, ObjectManager)
            .serve_at(AGENT_PATH, agent)
            .build()
            .await?;
        Ok(Agent { conn })
    }

    pub fn connection(&self) -> &Connection {
        &self.conn
    }

    /// Register with the router that owns capability slot `slot` (e.g. `org.agentbus.cap.Plan1`).
    pub async fn join_slot(&self, slot: &str) -> Result<()> {
        self.conn
            .call_method(Some(slot), AGENT_PATH, Some(ROUTER_IFACE), "Register", &())
            .await?;
        Ok(())
    }
}
