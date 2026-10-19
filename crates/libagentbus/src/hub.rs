//! The hub: one process and bus name ([`HUB_NAME`]) serving both topics
//! (`dev.agentbus.Topics1`) and functions (`dev.agentbus.Functions1`).

use futures_util::StreamExt;
use zbus::{Connection, connection, fdo::DBusProxy};

use crate::{
    Result,
    functions::{self, FUNCTIONS_PATH, Pins},
    topics::{self, TOPICS_PATH},
};

pub const HUB_NAME: &str = "dev.agentbus.Hub";

/// A running hub. Dropping it closes its connection.
pub struct Hub {
    conn: Connection,
}

impl Hub {
    pub fn connection(&self) -> &Connection {
        &self.conn
    }
}

/// Serve the hub under `bus_name`, with pins from the default `functions.conf`.
pub async fn serve(bus_name: &str) -> Result<Hub> {
    let pins = match functions::default_pins_path() {
        Some(path) => functions::load_pins(&path)?,
        None => Pins::new(),
    };
    serve_with_pins(bus_name, pins).await
}

pub async fn serve_with_pins(bus_name: &str, pins: Pins) -> Result<Hub> {
    let (topics_obj, topics_state) = topics::hub_object();
    let (functions_obj, functions_state) = functions::hub_object(pins);
    let conn = connection::Builder::session()
        .name(bus_name)
        .serve_at(TOPICS_PATH, topics_obj)
        .serve_at(FUNCTIONS_PATH, functions_obj)
        .build()
        .await?;

    // Subscriptions, registrations and calls are leases on the peer's connection.
    let mut gone = DBusProxy::new(&conn)
        .await?
        .receive_name_owner_changed()
        .await?;
    let watcher = conn.clone();
    tokio::spawn(async move {
        while let Some(signal) = gone.next().await {
            let Ok(args) = signal.args() else { continue };
            if args.new_owner().is_some() || !args.name().starts_with(':') {
                continue;
            }
            let name = args.name().to_string();
            if let Err(e) = topics::drop_owner(&watcher, &topics_state, &name).await {
                eprintln!("agentbus hub: could not drop subscriptions of {name}: {e}");
            }
            if let Err(e) = functions::drop_owner(&watcher, &functions_state, &name).await {
                eprintln!("agentbus hub: could not drop functions of {name}: {e}");
            }
        }
    });
    Ok(Hub { conn })
}
