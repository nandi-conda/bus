use std::fmt;

#[derive(Debug)]
pub enum Error {
    /// The bus or a remote peer reported an error.
    Bus(zbus::Error),
    /// A value could not be carried across the D-Bus / JSON boundary.
    Value(String),
    /// A task-level failure: invalid spec, unknown question, task canceled, ...
    Task(String),
}

pub type Result<T> = std::result::Result<T, Error>;

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Bus(e) => write!(f, "bus error: {e}"),
            Error::Value(m) => write!(f, "value error: {m}"),
            Error::Task(m) => write!(f, "task error: {m}"),
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
            Error::Value(m) | Error::Task(m) => zbus::fdo::Error::InvalidArgs(m),
        }
    }
}
