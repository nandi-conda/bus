use std::fmt;

#[derive(Debug)]
pub enum Error {
    /// The bus or a remote peer reported an error.
    Bus(zbus::Error),
    /// A value could not be carried across the D-Bus / JSON boundary.
    Value(String),
    /// A function call failed: `name` is the D-Bus error name (e.g.
    /// `org.freedesktop.DBus.Error.Timeout`, `org.freedesktop.DBus.Error.ServiceUnknown` when
    /// nobody provides the function), `message` says why.
    Call { name: String, message: String },
    /// The stream or subscription is closed.
    Closed(String),
}

pub type Result<T> = std::result::Result<T, Error>;

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Bus(e) => write!(f, "bus error: {e}"),
            Error::Value(m) => write!(f, "value error: {m}"),
            Error::Call { name, message } => write!(f, "{name}: {message}"),
            Error::Closed(m) => write!(f, "closed: {m}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<zbus::Error> for Error {
    fn from(e: zbus::Error) -> Self {
        Error::Bus(e)
    }
}

impl From<zbus::fdo::Error> for Error {
    fn from(e: zbus::fdo::Error) -> Self {
        Error::Bus(e.into())
    }
}

impl From<Error> for zbus::fdo::Error {
    fn from(e: Error) -> Self {
        match e {
            Error::Bus(e) => e.into(),
            Error::Value(m) => zbus::fdo::Error::InvalidArgs(m),
            Error::Call { message, .. } => zbus::fdo::Error::Failed(message),
            Error::Closed(m) => zbus::fdo::Error::Failed(m),
        }
    }
}
