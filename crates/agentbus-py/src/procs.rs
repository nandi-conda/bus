//! Python glue for `agentbus::functions`: a proc hosts functions and calls others by name.

use std::{sync::Arc, time::Duration};

use agentbus::{
    functions::{self, CallInfo},
    hub::HUB_NAME,
};
use pyo3::{prelude::*, types::PyDict};
use serde_json::Value as Json;

use crate::{block, bus_err, session, to_json, to_py, topics::Topics};

/// What a function handler learns about its call.
#[pyclass(frozen, name = "CallInfo")]
pub struct PyCallInfo {
    info: CallInfo,
}

#[pymethods]
impl PyCallInfo {
    #[getter]
    fn call_id(&self) -> &str {
        &self.info.call_id
    }

    /// The calling app's unique bus name.
    #[getter]
    fn caller(&self) -> &str {
        &self.info.caller
    }

    /// True once the hub canceled the call (caller left, or deadline passed).
    #[getter]
    fn cancelled(&self) -> bool {
        self.info.is_cancelled()
    }
}

fn run_handler(
    py: Python<'_>,
    handler: &Py<PyAny>,
    args: Json,
    info: CallInfo,
) -> Result<Json, String> {
    let call = || -> PyResult<Py<PyAny>> {
        let args = to_py(py, &args)?;
        handler.call1(py, (args, PyCallInfo { info }))
    };
    let ret = call().map_err(|e| e.to_string())?;
    match to_json(py, ret.bind(py)) {
        Ok(d @ Json::Object(_)) => Ok(d),
        Ok(other) => Err(format!("handler returned {other}, not a dict")),
        Err(e) => Err(format!("handler returned an unserializable value: {e}")),
    }
}

/// A proc's own bus connection: hosts functions, calls functions, and (via `.topics`)
/// publishes and subscribes under the same identity.
#[pyclass(frozen, name = "Procs")]
pub struct Procs {
    procs: functions::Procs,
    #[pyo3(get)]
    topics: Py<Topics>,
}

#[pymethods]
impl Procs {
    #[new]
    #[pyo3(signature = (hub=HUB_NAME))]
    fn new(py: Python<'_>, hub: &str) -> PyResult<Self> {
        // Make sure the session bus is reachable before building on it.
        drop(session(py)?);
        let procs = block(py, functions::Procs::connect(hub))?.map_err(bus_err)?;
        let topics = Py::new(py, Topics::on(procs.connection().clone(), hub))?;
        Ok(Procs { procs, topics })
    }

    /// This proc's unique bus name.
    #[getter]
    fn unique_name(&self) -> String {
        self.procs
            .connection()
            .unique_name()
            .map(|n| n.to_string())
            .unwrap_or_default()
    }

    /// Provide `name`. `handler(args: dict, info: CallInfo) -> dict` runs on a worker thread per
    /// call. `schema` is "" or a JSON string {"args": ..., "result": ...} the hub enforces.
    #[pyo3(signature = (name, handler, schema=""))]
    fn provide(
        &self,
        py: Python<'_>,
        name: &str,
        handler: Py<PyAny>,
        schema: &str,
    ) -> PyResult<()> {
        let handler = Arc::new(handler);
        let h = functions::function(move |args, info| {
            let handler = handler.clone();
            async move {
                let run = tokio::task::spawn_blocking(move || {
                    Python::attach(|py| run_handler(py, &handler, args, info))
                });
                run.await
                    .unwrap_or_else(|e| Err(format!("handler panicked: {e}")))
            }
        });
        block(py, self.procs.provide(name, schema, h))?.map_err(bus_err)
    }

    fn withdraw(&self, py: Python<'_>, name: &str) -> PyResult<()> {
        block(py, self.procs.withdraw(name))?.map_err(bus_err)
    }

    /// Call function `name` with an args dict. Raises NoProvider, TimeoutError or CallError.
    #[pyo3(signature = (name, args=None, timeout=None))]
    fn call(
        &self,
        py: Python<'_>,
        name: &str,
        args: Option<&Bound<'_, PyDict>>,
        timeout: Option<f64>,
    ) -> PyResult<Py<PyAny>> {
        let args = match args {
            Some(a) => to_json(py, a.as_any())?,
            None => Json::Object(Default::default()),
        };
        let timeout = timeout.map(Duration::from_secs_f64);
        let conn = self.procs.connection();
        let result = block(
            py,
            functions::call(conn, self.procs.hub(), name, &args, timeout),
        )?
        .map_err(bus_err)?;
        to_py(py, &result)
    }

    /// Every function with at least one provider.
    fn functions(&self, py: Python<'_>) -> PyResult<Vec<String>> {
        block(
            py,
            functions::functions(self.procs.connection(), self.procs.hub()),
        )?
        .map_err(bus_err)
    }

    /// [(unique name, stats dict)] for each provider of `name`.
    fn providers(&self, py: Python<'_>, name: &str) -> PyResult<Vec<(String, Py<PyAny>)>> {
        let list = block(
            py,
            functions::providers(self.procs.connection(), self.procs.hub(), name),
        )?
        .map_err(bus_err)?;
        list.into_iter()
            .map(|(o, s)| Ok((o, to_py(py, &s)?)))
            .collect()
    }

    /// Serve until interrupted (Ctrl-C raises KeyboardInterrupt).
    fn run_forever(&self, py: Python<'_>) -> PyResult<()> {
        crate::run_forever(py)
    }
}
