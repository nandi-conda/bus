//! Python glue for libagentbus. The API is synchronous: every call drives the shared Tokio
//! runtime with the GIL released, and checks for Ctrl-C while it waits.

use std::{
    future::Future,
    sync::{Arc, LazyLock},
    time::Duration,
};

use agentbus::{Outcome, RemoteTask, TaskContext, TaskEvent, TaskHandle, TaskSpec};
use pyo3::{
    create_exception,
    exceptions::{PyException, PyTimeoutError},
    prelude::*,
    types::PyDict,
};
use serde_json::Value as Json;

mod topics;

create_exception!(
    agentbus,
    BusError,
    PyException,
    "A D-Bus or task error from libagentbus."
);
create_exception!(
    agentbus,
    Rejected,
    PyException,
    "Raise from a task handler to decline the task."
);

static RT: LazyLock<tokio::runtime::Runtime> = LazyLock::new(|| {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("could not start the agentbus Tokio runtime")
});

fn bus_err(e: agentbus::Error) -> PyErr {
    BusError::new_err(e.to_string())
}

/// Run `fut` to completion with the GIL released, waking every 100ms to let Python handle
/// signals such as Ctrl-C.
fn block<F>(py: Python<'_>, fut: F) -> PyResult<F::Output>
where
    F: Future + Send,
    F::Output: Send,
{
    let mut fut = Box::pin(fut);
    loop {
        let step = py.detach(|| {
            RT.handle().block_on(async {
                tokio::time::timeout(Duration::from_millis(100), &mut fut).await
            })
        });
        match step {
            Ok(out) => return Ok(out),
            Err(_) => py.check_signals()?,
        }
    }
}

fn to_json(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Json> {
    let text: String = py
        .import("json")?
        .call_method1("dumps", (obj,))?
        .extract()?;
    serde_json::from_str(&text).map_err(|e| BusError::new_err(e.to_string()))
}

fn to_py(py: Python<'_>, json: &Json) -> PyResult<Py<PyAny>> {
    Ok(py
        .import("json")?
        .call_method1("loads", (json.to_string(),))?
        .unbind())
}

fn event_to_py(py: Python<'_>, event: TaskEvent) -> PyResult<Py<PyAny>> {
    let d = PyDict::new(py);
    match event {
        TaskEvent::Progress { pct, note } => {
            d.set_item("type", "progress")?;
            d.set_item("pct", pct)?;
            d.set_item("note", note)?;
        }
        TaskEvent::Question { id, text, schema } => {
            d.set_item("type", "question")?;
            d.set_item("id", id)?;
            d.set_item("text", text)?;
            d.set_item("schema", schema)?;
        }
        TaskEvent::Log { level, msg } => {
            d.set_item("type", "log")?;
            d.set_item("level", level)?;
            d.set_item("msg", msg)?;
        }
        TaskEvent::Finished(state) => {
            d.set_item("type", "finished")?;
            d.set_item("state", state.as_str())?;
        }
    }
    Ok(d.into_any().unbind())
}

/// A session-bus connection for submitting tasks.
#[pyclass(frozen)]
struct Client {
    conn: agentbus::zbus::Connection,
}

#[pymethods]
impl Client {
    #[new]
    fn new(py: Python<'_>) -> PyResult<Self> {
        let conn =
            block(py, agentbus::zbus::Connection::session())?.map_err(|e| bus_err(e.into()))?;
        Ok(Client { conn })
    }

    /// Submit a task to an agent or capability slot. Returns a `Task`.
    #[pyo3(signature = (dest, task_class, goal, params=None))]
    fn submit(
        &self,
        py: Python<'_>,
        dest: &str,
        task_class: &str,
        goal: &str,
        params: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Task> {
        let mut spec = TaskSpec::new(task_class, goal);
        if let Some(p) = params {
            spec = spec.params(to_json(py, p.as_any())?);
        }
        let remote = block(py, agentbus::submit(&self.conn, dest, &spec))?.map_err(bus_err)?;
        let handle = remote.handle().clone();
        Ok(Task {
            handle,
            remote: tokio::sync::Mutex::new(remote),
        })
    }
}

/// A submitted task. Iterate it for event dicts until it finishes.
#[pyclass(frozen)]
struct Task {
    handle: TaskHandle,
    remote: tokio::sync::Mutex<RemoteTask>,
}

#[pymethods]
impl Task {
    #[getter]
    fn path(&self) -> &str {
        self.handle.path()
    }

    #[getter]
    fn owner(&self) -> &str {
        self.handle.owner()
    }

    #[getter]
    fn state(&self, py: Python<'_>) -> PyResult<&'static str> {
        Ok(block(py, self.handle.state())?.map_err(bus_err)?.as_str())
    }

    /// Next event dict, or None once the task has finished. Raises TimeoutError after
    /// `timeout` seconds.
    #[pyo3(signature = (timeout=None))]
    fn next_event(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<Option<Py<PyAny>>> {
        let next = async {
            let mut remote = self.remote.lock().await;
            match timeout {
                Some(t) => tokio::time::timeout(Duration::from_secs_f64(t), remote.next_event())
                    .await
                    .ok(),
                None => Some(remote.next_event().await),
            }
        };
        match block(py, next)? {
            None => Err(PyTimeoutError::new_err("no task event before the timeout")),
            Some(event) => event
                .map_err(bus_err)?
                .map(|e| event_to_py(py, e))
                .transpose(),
        }
    }

    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        self.next_event(py, None)
    }

    /// Answer a question the agent asked.
    fn respond(
        &self,
        py: Python<'_>,
        question_id: &str,
        answer: &Bound<'_, PyDict>,
    ) -> PyResult<()> {
        let answer = to_json(py, answer.as_any())?;
        block(py, self.handle.respond(question_id, &answer))?.map_err(bus_err)
    }

    #[pyo3(signature = (reason=""))]
    fn cancel(&self, py: Python<'_>, reason: &str) -> PyResult<()> {
        block(py, self.handle.cancel(reason))?.map_err(bus_err)
    }

    /// `{"state", "deliverable", "report"}` once the task is finished.
    fn result(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let r = block(py, self.handle.result())?.map_err(bus_err)?;
        let d = PyDict::new(py);
        d.set_item("state", r.state.as_str())?;
        d.set_item("deliverable", to_py(py, &r.deliverable)?)?;
        d.set_item("report", to_py(py, &r.report)?)?;
        Ok(d.into_any().unbind())
    }
}

/// What a Python task handler receives.
#[pyclass(frozen, name = "TaskContext")]
struct PyTaskContext {
    ctx: TaskContext,
}

#[pymethods]
impl PyTaskContext {
    #[getter]
    fn path(&self) -> String {
        self.ctx.path()
    }

    #[getter]
    fn task_class(&self) -> &str {
        &self.ctx.spec().class
    }

    #[getter]
    fn goal(&self) -> &str {
        &self.ctx.spec().goal
    }

    #[getter]
    fn params(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        to_py(py, &self.ctx.spec().params)
    }

    #[getter]
    fn cancelled(&self) -> bool {
        self.ctx.is_cancelled()
    }

    #[pyo3(signature = (pct, note=""))]
    fn progress(&self, py: Python<'_>, pct: f64, note: &str) -> PyResult<()> {
        block(py, self.ctx.progress(pct, note))?.map_err(bus_err)
    }

    #[pyo3(signature = (msg, level="info"))]
    fn log(&self, py: Python<'_>, msg: &str, level: &str) -> PyResult<()> {
        block(py, self.ctx.log(level, msg))?.map_err(bus_err)
    }

    /// Ask the caller and wait for their answer dict.
    #[pyo3(signature = (text, schema=""))]
    fn ask(&self, py: Python<'_>, text: &str, schema: &str) -> PyResult<Py<PyAny>> {
        let answer = block(py, self.ctx.ask(text, schema))?.map_err(bus_err)?;
        to_py(py, &answer)
    }
}

fn run_handler(py: Python<'_>, handler: &Py<PyAny>, ctx: TaskContext) -> Outcome {
    let result = Py::new(py, PyTaskContext { ctx }).and_then(|c| handler.call1(py, (c,)));
    match result {
        Ok(ret) if ret.is_none(py) => Outcome::Completed(Json::Object(Default::default())),
        Ok(ret) => match to_json(py, ret.bind(py)) {
            Ok(d @ Json::Object(_)) => Outcome::Completed(d),
            Ok(other) => Outcome::Failed(format!("handler returned {other}, not a dict")),
            Err(e) => Outcome::Failed(format!("handler returned an unserializable value: {e}")),
        },
        Err(e) if e.is_instance_of::<Rejected>(py) => Outcome::Rejected(e.value(py).to_string()),
        Err(e) => Outcome::Failed(e.to_string()),
    }
}

/// Serve tasks under a bus name. `handler(ctx)` runs on a worker thread per task and returns
/// the deliverable dict; raising `Rejected` declines, any other exception fails the task.
#[pyclass(frozen)]
struct Agent {
    agent: agentbus::Agent,
}

#[pymethods]
impl Agent {
    #[new]
    #[pyo3(signature = (bus_name, task_classes, handler))]
    fn new(
        py: Python<'_>,
        bus_name: &str,
        task_classes: Vec<String>,
        handler: Py<PyAny>,
    ) -> PyResult<Self> {
        let handler = Arc::new(handler);
        let h = agentbus::handler(move |ctx| {
            let handler = handler.clone();
            async move {
                let run = tokio::task::spawn_blocking(move || {
                    Python::attach(|py| run_handler(py, &handler, ctx))
                });
                run.await
                    .unwrap_or_else(|e| Outcome::Failed(format!("handler panicked: {e}")))
            }
        });
        let agent =
            block(py, agentbus::Agent::serve(bus_name, task_classes, h))?.map_err(bus_err)?;
        Ok(Agent { agent })
    }

    /// Join the capability slot owned by a router, e.g. "org.agentbus.cap.Plan1".
    fn join_slot(&self, py: Python<'_>, slot: &str) -> PyResult<()> {
        block(py, self.agent.join_slot(slot))?.map_err(bus_err)
    }

    /// Serve until interrupted (Ctrl-C raises KeyboardInterrupt).
    fn run_forever(&self, py: Python<'_>) -> PyResult<()> {
        block(py, std::future::pending::<()>())
    }
}

#[pymodule(name = "agentbus")]
fn agentbus_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Client>()?;
    m.add_class::<Task>()?;
    m.add_class::<Agent>()?;
    m.add_class::<PyTaskContext>()?;
    m.add_class::<topics::TopicsHub>()?;
    m.add_class::<topics::Topics>()?;
    m.add_class::<topics::PySubscription>()?;
    m.add_class::<topics::PyDemandWatch>()?;
    m.add_function(wrap_pyfunction!(topics::pattern_matches, m)?)?;
    m.add("TOPICS_NAME", agentbus::topics::TOPICS_NAME)?;
    m.add("BusError", m.py().get_type::<BusError>())?;
    m.add("Rejected", m.py().get_type::<Rejected>())?;
    Ok(())
}
