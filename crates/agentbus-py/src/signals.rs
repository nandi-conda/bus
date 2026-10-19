//! Python glue for `agentbus::signals`: broadcast request/response signals without a hub.

use agentbus::signals::{self, Listener, Signal};
use pyo3::{prelude::*, types::PyDict, types::PyTuple};

use crate::{block, block_for, bus_err, session, to_json, to_py};

fn signal_to_py(py: Python<'_>, s: Signal) -> PyResult<Py<PyAny>> {
    let d = PyDict::new(py);
    d.set_item("sender", s.sender)?;
    d.set_item("path", s.path)?;
    d.set_item("interface", s.interface)?;
    d.set_item("member", s.member)?;
    let args = s
        .args
        .iter()
        .map(|a| to_py(py, a))
        .collect::<PyResult<Vec<_>>>()?;
    d.set_item("args", args)?;
    Ok(d.into_any().unbind())
}

/// A session-bus connection for broadcasting and listening to plain signals.
#[pyclass(frozen)]
pub struct Signals {
    conn: agentbus::zbus::Connection,
}

#[pymethods]
impl Signals {
    #[new]
    fn new(py: Python<'_>) -> PyResult<Self> {
        Ok(Signals { conn: session(py)? })
    }

    #[getter]
    fn unique_name(&self) -> String {
        self.conn
            .unique_name()
            .map(|n| n.to_string())
            .unwrap_or_default()
    }

    /// Emit `iface.member(*args)` from `path` to every listener. str -> s, int -> x,
    /// float -> d, bool -> b, list of str -> as, list of int -> ax, dict -> a{sv}.
    #[pyo3(signature = (path, iface, member, *args))]
    fn broadcast(
        &self,
        py: Python<'_>,
        path: &str,
        iface: &str,
        member: &str,
        args: &Bound<'_, PyTuple>,
    ) -> PyResult<()> {
        let args = args
            .iter()
            .map(|a| to_json(py, &a))
            .collect::<PyResult<Vec<_>>>()?;
        block(
            py,
            signals::broadcast(&self.conn, path, iface, member, &args),
        )?
        .map_err(bus_err)
    }

    /// Listen for signals on `path` and `iface` (and `member`, if given). Iterate the result
    /// for dicts: sender, path, interface, member, args.
    #[pyo3(signature = (path, iface, member=None))]
    fn listen(
        &self,
        py: Python<'_>,
        path: &str,
        iface: &str,
        member: Option<&str>,
    ) -> PyResult<PyListener> {
        let listener =
            block(py, signals::listen(&self.conn, path, iface, member))?.map_err(bus_err)?;
        Ok(PyListener {
            listener: tokio::sync::Mutex::new(Some(listener)),
        })
    }
}

#[pyclass(frozen, name = "Listener")]
pub struct PyListener {
    listener: tokio::sync::Mutex<Option<Listener>>,
}

#[pymethods]
impl PyListener {
    /// Next signal dict. Raises TimeoutError after `timeout` seconds.
    #[pyo3(signature = (timeout=None))]
    fn next_signal(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<Py<PyAny>> {
        let signal = block_for(py, timeout, async {
            match self.listener.lock().await.as_mut() {
                Some(l) => l.next_signal().await,
                None => Err(agentbus::Error::Closed("listener closed".into())),
            }
        })?
        .map_err(bus_err)?;
        signal_to_py(py, signal)
    }

    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.next_signal(py, None)
    }

    /// Stop listening.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        block(py, async { self.listener.lock().await.take() })?;
        Ok(())
    }
}
