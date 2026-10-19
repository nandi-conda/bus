//! A planner agent that joins the `org.agentbus.cap.Plan1` slot.
//!
//! Usage: agent [BUS_NAME]   (default org.agentbus.agent.planner)

use std::time::Duration;

use agentbus::{
    Agent,
    demo::{PLAN_CLASS, PLAN_SLOT, plan},
    handler,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let name = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "org.agentbus.agent.planner".into());
    let agent = Agent::serve(&name, vec![PLAN_CLASS.into()], handler(plan)).await?;
    // The router may still be starting; give it a few seconds before giving up.
    let mut tries = 0;
    while let Err(e) = agent.join_slot(PLAN_SLOT).await {
        tries += 1;
        if tries == 50 {
            return Err(format!("could not join {PLAN_SLOT}: {e}").into());
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    println!("agent: {name} joined {PLAN_SLOT}");
    tokio::signal::ctrl_c().await?;
    Ok(())
}
