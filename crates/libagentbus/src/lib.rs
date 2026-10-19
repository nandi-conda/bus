//! libagentbus: networked procs on D-Bus.
//!
//! Procs are ordinary processes that host [`functions`] (named calls such as
//! `weather.current`) and publish [`topics`] (app-domain broadcasts such as `weather.alert` for
//! `Berlin`). Apps call a function or subscribe to a topic by name through the [`hub`], never
//! naming the proc. [`signals`] covers plain broadcast signals for request/response patterns
//! that need no hub.

mod error;
pub mod functions;
pub mod hub;
pub mod signals;
pub mod topics;
mod value;

pub use error::{Error, Result};
pub use value::{Dict, dict_to_json, json_to_dict};

pub use zbus;
