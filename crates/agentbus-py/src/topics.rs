//! Python glue for `agentbus::topics`: app-domain subscriptions on a hub.

use agentbus::{
    hub::{self, HUB_NAME},
    topics::{self, Demand, DemandWatch, Subscription, TopicEvent},
};
use pyo3::{prelude::*, types::PyDict};

use crate::{block, block_for, bus_err, session, to_json, to_py};

fn event_to_py(py: Python<'_>, e: TopicEvent) -> PyResult<Py<PyAny>> {
    let d = PyDict::new(py);
    d.set_item("topic", e.topic)?;
    d.set_item("subject", e.subject)?;
    d.set_item("payload", to_py(py, &e.payload)?)?;
    d.set_item("seq", e.seq)?;
    Ok(d.into_any().unbind())
}

fn demand_to_py(py: Python<'_>, d: Demand) -> PyResult<Py<PyAny>> {
    let out = PyDict::new(py);
    out.set_item("pattern", d.pattern)?;
    out.set_item("subject", d.subject)?;
    out.set_item("subscribers", d.subscribers)?;
    Ok(out.into_any().unbind())
}

/// Serve the hub (topics and functions) under `bus_name` until this object is dropped.
#[pyclass(frozen)]
pub struct Hub {
    _hub: hub::Hub,
}

#[pymethods]
impl Hub {
    #[new]
    #[pyo3(signature = (bus_name=HUB_NAME))]
    fn new(py: Python<'_>, bus_name: &str) -> PyResult<Self> {
        let hub = block(py, hub::serve(bus_name))?.map_err(bus_err)?;
        Ok(Hub { _hub: hub })
    }

    /// Serve until interrupted (Ctrl-C raises KeyboardInterrupt).
    fn run_forever(&self, py: Python<'_>) -> PyResult<()> {
        crate::run_forever(py)
    }
}

/// A session-bus connection to a topics hub, for apps and publishers.
#[pyclass(frozen)]
pub struct Topics {
    conn: agentbus::zbus::Connection,
    hub: String,
}

impl Topics {
    /// Topics over an existing connection, so a proc publishes under its own identity.
    pub fn on(conn: agentbus::zbus::Connection, hub: &str) -> Self {
        Topics {
            conn,
            hub: hub.to_string(),
        }
    }
}

#[pymethods]
impl Topics {
    #[new]
    #[pyo3(signature = (hub=HUB_NAME))]
    fn new(py: Python<'_>, hub: &str) -> PyResult<Self> {
        Ok(Topics::on(session(py)?, hub))
    }

    /// Subscribe to `pattern` ("weather.current", "weather.*" or "*") for `subject` ("" = any).
    /// Iterate the result for event dicts: topic, subject, payload, seq.
    #[pyo3(signature = (pattern, subject=""))]
    fn subscribe(&self, py: Python<'_>, pattern: &str, subject: &str) -> PyResult<PySubscription> {
        let sub = block(
            py,
            topics::subscribe(&self.conn, &self.hub, pattern, subject),
        )?
        .map_err(bus_err)?;
        Ok(PySubscription {
            path: sub.path().to_string(),
            sub: tokio::sync::Mutex::new(Some(sub)),
        })
    }

    /// Publish a payload dict on `topic` for `subject`. Returns how many subscribers got it.
    fn publish(
        &self,
        py: Python<'_>,
        topic: &str,
        subject: &str,
        payload: &Bound<'_, PyDict>,
    ) -> PyResult<u32> {
        let payload = to_json(py, payload.as_any())?;
        block(
            py,
            topics::publish(&self.conn, &self.hub, topic, subject, &payload),
        )?
        .map_err(bus_err)
    }

    /// Follow what apps are watching. `.current` is the demand right now; iterate for changes.
    fn watch_demand(&self, py: Python<'_>) -> PyResult<PyDemandWatch> {
        let mut watch = block(py, topics::watch_demand(&self.conn, &self.hub))?.map_err(bus_err)?;
        let current = std::mem::take(&mut watch.current)
            .into_iter()
            .map(|d| demand_to_py(py, d))
            .collect::<PyResult<_>>()?;
        Ok(PyDemandWatch {
            current,
            watch: tokio::sync::Mutex::new(watch),
        })
    }
}

#[pyclass(frozen, name = "Subscription")]
pub struct PySubscription {
    path: String,
    sub: tokio::sync::Mutex<Option<Subscription>>,
}

#[pymethods]
impl PySubscription {
    #[getter]
    fn path(&self) -> &str {
        &self.path
    }

    /// Next event dict. Raises TimeoutError after `timeout` seconds.
    #[pyo3(signature = (timeout=None))]
    fn next_event(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<Py<PyAny>> {
        let event = block_for(py, timeout, async {
            match self.sub.lock().await.as_mut() {
                Some(sub) => sub.next_event().await,
                None => Err(agentbus::Error::Closed("unsubscribed".into())),
            }
        })?
        .map_err(bus_err)?;
        event_to_py(py, event)
    }

    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.next_event(py, None)
    }

    fn unsubscribe(&self, py: Python<'_>) -> PyResult<()> {
        block(py, async {
            match self.sub.lock().await.take() {
                Some(sub) => sub.unsubscribe().await,
                None => Ok(()),
            }
        })?
        .map_err(bus_err)
    }
}

#[pyclass(frozen, name = "DemandWatch")]
pub struct PyDemandWatch {
    #[pyo3(get)]
    current: Vec<Py<PyAny>>,
    watch: tokio::sync::Mutex<DemandWatch>,
}

#[pymethods]
impl PyDemandWatch {
    /// Next demand change: pattern, subject, subscribers. Raises TimeoutError after `timeout`.
    #[pyo3(signature = (timeout=None))]
    fn next_change(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<Py<PyAny>> {
        let change = block_for(py, timeout, async {
            self.watch.lock().await.next_change().await
        })?
        .map_err(bus_err)?;
        demand_to_py(py, change)
    }

    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.next_change(py, None)
    }
}

/// Does `pattern` ("a.b", "a.*" or "*") cover `topic`?
#[pyfunction]
pub fn pattern_matches(pattern: &str, topic: &str) -> bool {
    topics::pattern_matches(pattern, topic)
}
