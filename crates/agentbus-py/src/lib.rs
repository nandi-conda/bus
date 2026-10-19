//! Python glue for libagentbus, built as `agentbus._native`; the `agentbus` package on top adds
//! the decorator API. Calls are synchronous: each drives the shared Tokio runtime with the GIL
//! released, and checks for Ctrl-C while it waits.

use std::{future::Future, sync::LazyLock, time::Duration};

use pyo3::{
    create_exception,
    exceptions::{PyException, PyTimeoutError},
    prelude::*,
};
use serde_json::Value as Json;

mod procs;
mod signals;
mod topics;

create_exception!(
    agentbus,
    BusError,
    PyException,
    "A D-Bus error from libagentbus."
);
create_exception!(agentbus, CallError, BusError, "A function call failed.");
create_exception!(
    agentbus,
    NoProvider,
    CallError,
    "Nobody provides the function."
);

static RT: LazyLock<tokio::runtime::Runtime> = LazyLock::new(|| {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("could not start the agentbus Tokio runtime")
});

fn bus_err(e: agentbus::Error) -> PyErr {
    match e {
        agentbus::Error::Call { name, message } => match name.as_str() {
            "org.freedesktop.DBus.Error.Timeout" => PyTimeoutError::new_err(message),
            "org.freedesktop.DBus.Error.ServiceUnknown" => NoProvider::new_err(message),
            _ => CallError::new_err(format!("{name}: {message}")),
        },
        other => BusError::new_err(other.to_string()),
    }
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

/// Wait for `fut`, or raise TimeoutError after `timeout` seconds.
fn block_for<F>(py: Python<'_>, timeout: Option<f64>, fut: F) -> PyResult<F::Output>
where
    F: Future + Send,
    F::Output: Send,
{
    let out = block(py, async {
        match timeout {
            Some(t) => tokio::time::timeout(Duration::from_secs_f64(t), fut)
                .await
                .ok(),
            None => Some(fut.await),
        }
    })?;
    out.ok_or_else(|| PyTimeoutError::new_err("nothing arrived before the timeout"))
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

fn session(py: Python<'_>) -> PyResult<agentbus::zbus::Connection> {
    block(py, agentbus::zbus::Connection::session())?.map_err(|e| bus_err(e.into()))
}

/// Serve until interrupted (Ctrl-C raises KeyboardInterrupt).
fn run_forever(py: Python<'_>) -> PyResult<()> {
    block(py, std::future::pending::<()>())
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<topics::Hub>()?;
    m.add_class::<topics::Topics>()?;
    m.add_class::<topics::PySubscription>()?;
    m.add_class::<topics::PyDemandWatch>()?;
    m.add_class::<procs::Procs>()?;
    m.add_class::<procs::PyCallInfo>()?;
    m.add_class::<signals::Signals>()?;
    m.add_class::<signals::PyListener>()?;
    m.add_function(wrap_pyfunction!(topics::pattern_matches, m)?)?;
    m.add("HUB_NAME", agentbus::hub::HUB_NAME)?;
    m.add("BusError", m.py().get_type::<BusError>())?;
    m.add("CallError", m.py().get_type::<CallError>())?;
    m.add("NoProvider", m.py().get_type::<NoProvider>())?;
    Ok(())
}
