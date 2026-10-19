//! Own `dev.agentbus.Topics`, the hub apps subscribe to and services publish through.

use agentbus::topics::{self, TOPICS_NAME};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let _hub = topics::serve(TOPICS_NAME).await?;
    println!("hub: serving {TOPICS_NAME}");
    tokio::signal::ctrl_c().await?;
    Ok(())
}
