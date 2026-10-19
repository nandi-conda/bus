//! Own the `org.agentbus.cap.Plan1` capability slot and route its tasks to registered agents.

use agentbus::demo::PLAN_SLOT;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let slot = std::env::args()
        .nth(1)
        .unwrap_or_else(|| PLAN_SLOT.to_string());
    let _router = agentbus::router::serve(&slot).await?;
    println!("router: serving {slot}");
    tokio::signal::ctrl_c().await?;
    Ok(())
}
